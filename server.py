#!/usr/bin/env python3
"""Local MP4 downloader: serves the UI and streams direct .mp4 URLs to disk."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

HOST = "127.0.0.1"
PORT = 8791
CHUNK = 256 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ROOT = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path.home() / "Downloads" / "MP4 Downloader"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
MAX_HISTORY = 40


def send_json(handler: SimpleHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: SimpleHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > 1_000_000:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "url": job["url"],
        "filename": job["filename"],
        "status": job["status"],
        "received": job["received"],
        "total": job["total"],
        "speed": job["speed"],
        "error": job["error"],
        "path": job["path"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }


def safe_filename(name: str) -> str:
    name = unquote(name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    if not name:
        name = "video"
    stem, _, ext = name.rpartition(".")
    if ext.lower() != "mp4":
        name = f"{name}.mp4"
    if len(name) > 180:
        name = name[:176] + ".mp4"
    return name


def filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    candidate = path.rsplit("/", 1)[-1] if path else ""
    return safe_filename(candidate or "video.mp4")


def filename_from_disposition(header: str | None) -> str | None:
    if not header:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', header, re.I)
    if not match:
        return None
    return safe_filename(match.group(1).strip())


def unique_path(folder: Path, filename: str) -> Path:
    dest = folder / filename
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    for i in range(2, 1000):
        candidate = folder / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return folder / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"


def validate_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Use an http or https link."
    if not parsed.netloc:
        return "That URL is missing a host."
    host = parsed.hostname or ""
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return "Local URLs are not allowed."
    return None


def looks_like_html(content_type: str, peek: bytes) -> bool:
    ctype = content_type.lower()
    if "text/html" in ctype or "application/xhtml" in ctype:
        return True
    head = peek.lstrip()[:64].lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def looks_like_mp4(peek: bytes) -> bool:
    return len(peek) >= 8 and peek[4:8] == b"ftyp"


def is_video_type(content_type: str, url: str) -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in {"video/mp4", "video/quicktime", "application/mp4", "application/octet-stream"}:
        return True
    if ctype.startswith("video/"):
        return True
    path = urlparse(url).path.lower()
    return path.endswith(".mp4") or path.endswith(".m4v") or path.endswith(".mov")


def update_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(fields)


def run_download(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        url = job["url"]
        cancel = job["cancel"]
        dest = Path(job["path"])
        part = dest.with_suffix(dest.suffix + ".part")

    update_job(job_id, status="running", started_at=time.time())
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(request, timeout=30) as response:
            if cancel.is_set():
                raise InterruptedError("cancelled")

            content_type = response.headers.get("Content-Type", "")
            total = int(response.headers.get("Content-Length") or 0)
            hinted = filename_from_disposition(response.headers.get("Content-Disposition"))
            if hinted:
                dest = unique_path(DOWNLOAD_DIR, hinted)
                part = dest.with_suffix(dest.suffix + ".part")
                update_job(job_id, filename=dest.name, path=str(dest))

            update_job(job_id, total=total)

            received = 0
            speed = 0.0
            window_bytes = 0
            window_start = time.monotonic()
            peek = b""
            checked = False

            with open(part, "wb") as handle:
                while True:
                    if cancel.is_set():
                        raise InterruptedError("cancelled")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    if not checked:
                        peek += chunk
                        if len(peek) >= 64 or len(chunk) < CHUNK:
                            if looks_like_html(content_type, peek):
                                raise ValueError("That link is a web page, not an MP4 file.")
                            if not is_video_type(content_type, url) and not looks_like_mp4(peek):
                                raise ValueError("That file does not look like an MP4.")
                            if len(peek) >= 8 and not looks_like_mp4(peek) and "video/" not in content_type.lower():
                                raise ValueError("That file does not look like an MP4.")
                            checked = True
                    handle.write(chunk)
                    received += len(chunk)
                    window_bytes += len(chunk)
                    elapsed = time.monotonic() - window_start
                    if elapsed >= 0.4:
                        speed = window_bytes / elapsed
                        window_bytes = 0
                        window_start = time.monotonic()
                    update_job(job_id, received=received, total=total, speed=speed)

            if received == 0:
                raise ValueError("The server sent an empty file.")

        part.replace(dest)
        update_job(
            job_id,
            status="done",
            received=received,
            speed=0,
            finished_at=time.time(),
            path=str(dest),
        )
    except InterruptedError:
        if part.exists():
            part.unlink(missing_ok=True)
        update_job(job_id, status="cancelled", speed=0, finished_at=time.time())
    except HTTPError as exc:
        if part.exists():
            part.unlink(missing_ok=True)
        update_job(
            job_id,
            status="error",
            error=f"Download failed ({exc.code}).",
            speed=0,
            finished_at=time.time(),
        )
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        if part.exists():
            part.unlink(missing_ok=True)
        message = str(exc).strip() or "Download failed."
        update_job(job_id, status="error", error=message, speed=0, finished_at=time.time())


def start_job(url: str, filename: str | None) -> dict[str, Any]:
    problem = validate_url(url)
    if problem:
        raise ValueError(problem)

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename) if filename else filename_from_url(url)
    dest = unique_path(DOWNLOAD_DIR, name)
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "url": url,
        "filename": dest.name,
        "status": "queued",
        "received": 0,
        "total": 0,
        "speed": 0.0,
        "error": "",
        "path": str(dest),
        "started_at": None,
        "finished_at": None,
        "cancel": threading.Event(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
        finished = [jid for jid, item in JOBS.items() if item["status"] in {"done", "error", "cancelled"}]
        while len(JOBS) > MAX_HISTORY and finished:
            JOBS.pop(finished.pop(0), None)
    threading.Thread(target=run_download, args=(job_id,), daemon=True).start()
    return job


def inside_download_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(DOWNLOAD_DIR.resolve())
        return True
    except ValueError:
        return False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        if args and str(args[0]).startswith("GET /api/jobs"):
            return
        super().log_message(format, *args)

    def do_GET(self) -> None:
        if self.path == "/api/config":
            send_json(self, {"folder": str(DOWNLOAD_DIR), "port": PORT})
            return
        if self.path == "/api/jobs":
            with JOBS_LOCK:
                jobs = [public_job(job) for job in reversed(list(JOBS.values()))]
            send_json(self, {"jobs": jobs})
            return
        if self.path.startswith("/api/jobs/"):
            job_id = self.path.split("/api/jobs/", 1)[1].split("?")[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                send_json(self, {"error": "Job not found."}, 404)
                return
            send_json(self, public_job(job))
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/api/download":
            data = read_json(self)
            url = str(data.get("url") or "").strip()
            filename = str(data.get("filename") or "").strip() or None
            if not url:
                send_json(self, {"error": "Paste an MP4 URL first."}, 400)
                return
            try:
                job = start_job(url, filename)
            except ValueError as exc:
                send_json(self, {"error": str(exc)}, 400)
                return
            send_json(self, public_job(job), 202)
            return

        if self.path.startswith("/api/jobs/") and self.path.endswith("/cancel"):
            job_id = self.path.split("/api/jobs/", 1)[1].removesuffix("/cancel").strip("/")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                send_json(self, {"error": "Job not found."}, 404)
                return
            job["cancel"].set()
            send_json(self, public_job(job))
            return

        if self.path == "/api/open-folder":
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(DOWNLOAD_DIR)  # type: ignore[attr-defined]
            send_json(self, {"ok": True})
            return

        if self.path == "/api/open-file":
            data = read_json(self)
            path = Path(str(data.get("path") or ""))
            if not path.is_file() or not inside_download_dir(path):
                send_json(self, {"error": "File not found."}, 404)
                return
            os.startfile(path)  # type: ignore[attr-defined]
            send_json(self, {"ok": True})
            return

        send_json(self, {"error": "Unknown endpoint."}, 404)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"MP4 Downloader  http://{HOST}:{PORT}/", flush=True)
    print(f"Saving files to {DOWNLOAD_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()

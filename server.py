#!/usr/bin/env python3
"""Local YouTube → MP4 downloader."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

HOST = "127.0.0.1"
PORT = 8791
ROOT = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path.home() / "Downloads" / "MP4 Downloader"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
MAX_HISTORY = 40
QUALITIES = {"best", "1080", "720", "480", "360"}
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

FFMPEG_PATH: str | None = None


def ensure_deps() -> None:
    try:
        import imageio_ffmpeg  # noqa: F401
        import yt_dlp  # noqa: F401
    except ImportError:
        req = ROOT / "requirements.txt"
        print("Installing yt-dlp…", flush=True)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req)])


def ffmpeg_path() -> str:
    global FFMPEG_PATH
    if FFMPEG_PATH:
        return FFMPEG_PATH
    import imageio_ffmpeg

    print("Preparing ffmpeg (first run may take a minute)…", flush=True)
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    return FFMPEG_PATH


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
        "title": job.get("title") or job["filename"],
        "quality": job.get("quality") or "best",
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


def is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in YOUTUBE_HOSTS


def validate_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Use an http or https YouTube link."
    if not is_youtube(url):
        return "Paste a YouTube link (youtube.com or youtu.be)."
    return None


def format_selector(quality: str) -> str:
    mp4 = "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]"
    fallback = "bv*+ba/b"
    heights = {"1080": 1080, "720": 720, "480": 480, "360": 360}
    height = heights.get(quality)
    if not height:
        return f"{mp4}/{fallback}"
    cap = f"[height<={height}]"
    return (
        f"bv*{cap}[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*{cap}[ext=mp4]+ba[ext=m4a]/"
        f"b{cap}[ext=mp4]/"
        f"bv*{cap}+ba/"
        f"b{cap}"
    )


def update_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(fields)


def cleanup_sidecars(dest: Path) -> None:
    stem = dest.stem
    for path in dest.parent.glob(f"{stem}*"):
        name = path.name.lower()
        if path == dest:
            continue
        if name.endswith((".part", ".ytdl", ".tmp")) or ".f" in path.stem:
            try:
                path.unlink()
            except OSError:
                pass


def run_download(job_id: str) -> None:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled, DownloadError

    with JOBS_LOCK:
        job = JOBS[job_id]
        url = job["url"]
        cancel = job["cancel"]
        dest = Path(job["path"])
        quality = job.get("quality") or "best"

    update_job(job_id, status="running", started_at=time.time())
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    class Cancelled(DownloadCancelled):
        pass

    def hook(event: dict[str, Any]) -> None:
        if cancel.is_set():
            raise Cancelled("cancelled")
        status = event.get("status")
        if status == "downloading":
            total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
            update_job(
                job_id,
                received=int(event.get("downloaded_bytes") or 0),
                total=int(total or 0),
                speed=float(event.get("speed") or 0),
            )
            info = event.get("info_dict") or {}
            title = info.get("title")
            if title:
                update_job(job_id, title=title)
        elif status == "finished":
            update_job(job_id, speed=0)

    outtmpl = str(dest.with_suffix("")) + ".%(ext)s"
    opts = {
        "format": format_selector(quality),
        "merge_output_format": "mp4",
        "final_ext": "mp4",
        "outtmpl": outtmpl,
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "windowsfilenames": True,
        "restrictfilenames": False,
        "overwrites": True,
        "ffmpeg_location": ffmpeg_path(),
        "retries": 3,
        "fragment_retries": 3,
        "ignoreerrors": False,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if cancel.is_set():
                raise Cancelled("cancelled")
            if not info:
                raise ValueError("Could not read that YouTube video.")
            if info.get("_type") == "playlist":
                entries = [item for item in (info.get("entries") or []) if item]
                if not entries:
                    raise ValueError("That playlist has no videos.")
                info = entries[0]
            title = info.get("title") or dest.stem
            prepared = Path(ydl.prepare_filename(info))
            candidates = [prepared.with_suffix(".mp4"), prepared, dest, dest.with_suffix(".mp4")]
            final = next((path for path in candidates if path.exists() and path.stat().st_size > 0), None)
            if final is None:
                matches = sorted(
                    DOWNLOAD_DIR.glob(f"{dest.stem}.*"),
                    key=lambda path: path.stat().st_mtime,
                )
                mp4s = [path for path in matches if path.suffix.lower() == ".mp4"]
                final = mp4s[-1] if mp4s else None
            if final is None or not final.exists():
                raise ValueError("Download finished but no MP4 was written.")
            size = final.stat().st_size
            with JOBS_LOCK:
                custom = bool(JOBS[job_id].get("custom_name"))
            if not custom and title:
                wanted = unique_path(DOWNLOAD_DIR, safe_filename(f"{title}.mp4"))
                if wanted.resolve() != final.resolve():
                    final.replace(wanted)
                    final = wanted
                    size = final.stat().st_size
            update_job(
                job_id,
                status="done",
                filename=final.name,
                title=title,
                path=str(final),
                received=size,
                total=size,
                speed=0,
                finished_at=time.time(),
            )
            cleanup_sidecars(final)
    except Cancelled:
        cleanup_sidecars(dest)
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
        update_job(job_id, status="cancelled", speed=0, finished_at=time.time())
    except DownloadError as exc:
        cleanup_sidecars(dest)
        message = str(exc).strip() or "YouTube download failed."
        message = re.sub(r"^ERROR:\s*", "", message)
        if len(message) > 280:
            message = message[:277] + "..."
        update_job(job_id, status="error", error=message, speed=0, finished_at=time.time())
    except Exception as exc:
        cleanup_sidecars(dest)
        message = str(exc).strip() or "Download failed."
        update_job(job_id, status="error", error=message, speed=0, finished_at=time.time())


def start_job(url: str, filename: str | None, quality: str) -> dict[str, Any]:
    problem = validate_url(url)
    if problem:
        raise ValueError(problem)
    if quality not in QUALITIES:
        quality = "best"

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename) if filename else "youtube-video.mp4"
    dest = unique_path(DOWNLOAD_DIR, name)
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "url": url,
        "filename": dest.name,
        "title": dest.stem if filename else "Fetching video…",
        "quality": quality,
        "status": "queued",
        "received": 0,
        "total": 0,
        "speed": 0.0,
        "error": "",
        "path": str(dest),
        "custom_name": bool(filename),
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
            quality = str(data.get("quality") or "best").strip().lower()
            if not url:
                send_json(self, {"error": "Paste a YouTube link first."}, 400)
                return
            try:
                job = start_job(url, filename, quality)
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
    ensure_deps()
    ffmpeg_path()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"YouTube to MP4  http://{HOST}:{PORT}/", flush=True)
    print(f"Saving files to {DOWNLOAD_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        server.server_close()


if __name__ == "__main__":
    main()

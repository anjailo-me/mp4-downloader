const form = document.getElementById("form");
const urlInput = document.getElementById("url");
const filenameInput = document.getElementById("filename");
const qualityInput = document.getElementById("quality");
const downloadBtn = document.getElementById("download-btn");
const banner = document.getElementById("banner");
const jobsEl = document.getElementById("jobs");
const countPill = document.getElementById("count-pill");
const folderLabel = document.getElementById("folder-label");
const openFolderBtn = document.getElementById("open-folder");

let pollTimer = 0;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function showBanner(message) {
  banner.hidden = !message;
  banner.textContent = message || "";
}

function bytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let value = n;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value >= 10 || i === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[i]}`;
}

function percent(job) {
  if (!job.total) return job.status === "done" ? 100 : 8;
  return Math.min(100, Math.round((job.received / job.total) * 100));
}

function statusLabel(job) {
  if (job.status === "running") return "Downloading";
  if (job.status === "queued") return "Queued";
  if (job.status === "done") return "Done";
  if (job.status === "cancelled") return "Cancelled";
  return "Failed";
}

function render(jobs) {
  if (!jobs.length) {
    jobsEl.innerHTML = `<li class="empty">Paste a YouTube link and hit download.</li>`;
    countPill.textContent = "None yet";
    return;
  }
  countPill.textContent = `${jobs.length} file${jobs.length === 1 ? "" : "s"}`;
  jobsEl.innerHTML = jobs.map((job) => {
    const pct = percent(job);
    const speed = job.status === "running" && job.speed
      ? `${bytes(job.speed)}/s`
      : "";
    const size = job.total
      ? `${bytes(job.received)} / ${bytes(job.total)}`
      : bytes(job.received);
    const actions = [];
    if (job.status === "running" || job.status === "queued") {
      actions.push(`<button class="linkish" data-cancel="${esc(job.id)}" type="button">Cancel</button>`);
    }
    if (job.status === "done") {
      actions.push(`<button class="linkish" data-open="${esc(job.path)}" type="button">Open file</button>`);
    }
    const error = job.error ? `<p class="meta">${esc(job.error)}</p>` : "";
    const label = esc(job.title || job.filename);
    return `
      <li class="job">
        <div class="job-row">
          <span class="name" title="${label}">${label}</span>
          <span class="status ${esc(job.status)}">${statusLabel(job)}</span>
        </div>
        <div class="bar" aria-hidden="true"><div class="fill" style="width:${pct}%"></div></div>
        <div class="job-row">
          <span class="stats">${size}${speed ? ` · ${speed}` : ""}${job.total ? ` · ${pct}%` : ""}</span>
          <div class="job-actions">${actions.join("")}</div>
        </div>
        ${error}
      </li>
    `;
  }).join("");
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Request failed.");
  return data;
}

async function refresh() {
  try {
    const data = await api("/api/jobs");
    render(data.jobs || []);
    const busy = (data.jobs || []).some((job) => job.status === "running" || job.status === "queued");
    if (busy && !pollTimer) pollTimer = setInterval(refresh, 400);
    if (!busy && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = 0;
    }
  } catch {
    /* keep last render */
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showBanner("");
  downloadBtn.disabled = true;
  try {
    await api("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: urlInput.value.trim(),
        filename: filenameInput.value.trim(),
        quality: qualityInput.value,
      }),
    });
    filenameInput.value = "";
    await refresh();
  } catch (err) {
    showBanner(err.message);
  } finally {
    downloadBtn.disabled = false;
  }
});

jobsEl.addEventListener("click", async (event) => {
  const cancelId = event.target.dataset.cancel;
  const openPath = event.target.dataset.open;
  try {
    if (cancelId) {
      await api(`/api/jobs/${cancelId}/cancel`, { method: "POST" });
      await refresh();
    }
    if (openPath) {
      await api("/api/open-file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: openPath }),
      });
    }
  } catch (err) {
    showBanner(err.message);
  }
});

openFolderBtn.addEventListener("click", async () => {
  try {
    await api("/api/open-folder", { method: "POST" });
  } catch (err) {
    showBanner(err.message);
  }
});

(async () => {
  try {
    const config = await api("/api/config");
    folderLabel.textContent = `Saves to ${config.folder}`;
  } catch {
    folderLabel.textContent = "Saves to Downloads\\MP4 Downloader";
  }
  refresh();
  urlInput.focus();
})();

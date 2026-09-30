"""A small log-viewer web UI, served by this same FastAPI app (already
running on localhost:8000, so no separate service/port to manage) --
lets the backend AND frontend logs (backend/logging_config.py,
frontend/logging_config.py) be browsed, searched, and downloaded from a
browser instead of having to open the raw files or scroll back through a
closing console window. Frontend logs reach here via the docker-compose
read-only bind mount at /app/frontend_logs (the frontend runs natively on
the host, outside Docker, so this container can't see that path any
other way).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

from backend.logging_config import LOG_FILE as BACKEND_LOG_FILE

FRONTEND_LOG_FILE = Path("/app/frontend_logs/frontend.log")
DEFAULT_TAIL_LINES = 2000
MAX_TAIL_LINES = 20000

router = APIRouter(prefix="/logs", tags=["logs"])

LOG_SOURCES = {
    "backend": BACKEND_LOG_FILE,
    "frontend": FRONTEND_LOG_FILE,
}


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return f"(no log file yet at {path})"
    try:
        # Log files here are capped at 5MB by RotatingFileHandler, so
        # reading the whole thing and slicing lines in memory is fine --
        # no need for a seek-from-end byte-scan for a file this size.
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(could not read {path}: {exc})"
    all_lines = text.splitlines()
    return "\n".join(all_lines[-lines:])


@router.get("/{source}", response_class=PlainTextResponse)
def get_log(source: str, lines: int = DEFAULT_TAIL_LINES) -> str:
    path = LOG_SOURCES.get(source)
    if path is None:
        return f"Unknown log source {source!r}. Valid: {', '.join(LOG_SOURCES)}"
    lines = max(1, min(lines, MAX_TAIL_LINES))
    return _tail(path, lines)


@router.get("/{source}/download")
def download_log(source: str):
    path = LOG_SOURCES.get(source)
    if path is None or not path.exists():
        return PlainTextResponse(f"No log file for {source!r}", status_code=404)
    return PlainTextResponse(
        path.read_text(encoding="utf-8", errors="replace"),
        headers={"Content-Disposition": f'attachment; filename="{source}.log"'},
    )


@router.get("", response_class=HTMLResponse)
def viewer_page() -> str:
    return _PAGE_HTML


_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>StreamScribe_adaptive -- Logs</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0; background: #1a1a1a; color: #e8e8e8;
    font-family: "Segoe UI", system-ui, sans-serif;
    display: flex; flex-direction: column; height: 100vh;
  }
  header {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 16px; background: #242424; border-bottom: 1px solid #3a3a3a;
    flex-wrap: wrap;
  }
  h1 { font-size: 15px; font-weight: 600; margin: 0; margin-right: 8px; color: #fff; }
  .tabs { display: flex; gap: 4px; }
  .tab {
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
    background: #333; color: #ccc; border: 1px solid #444;
  }
  .tab.active { background: #3fbf50; color: #0a0a0a; border-color: #3fbf50; font-weight: 600; }
  input[type=text] {
    background: #2a2a2a; border: 1px solid #444; color: #eee; border-radius: 6px;
    padding: 6px 10px; font-size: 13px; min-width: 220px;
  }
  label { font-size: 13px; color: #bbb; display: flex; align-items: center; gap: 4px; }
  button {
    background: #333; color: #eee; border: 1px solid #444; border-radius: 6px;
    padding: 6px 12px; font-size: 13px; cursor: pointer;
  }
  button:hover { background: #3a3a3a; }
  a.dl { color: #7fbfff; font-size: 13px; text-decoration: none; }
  a.dl:hover { text-decoration: underline; }
  #status { font-size: 12px; color: #888; margin-left: auto; }
  main { flex: 1; overflow: auto; padding: 0; }
  pre {
    margin: 0; padding: 12px 16px; font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-word;
  }
  .line.hidden { display: none; }
  .lvl-ERROR, .lvl-CRITICAL { color: #ff6b6b; }
  .lvl-WARNING { color: #e0b400; }
  .lvl-INFO { color: #cfe8ff; }
</style>
</head>
<body>
<header>
  <h1>StreamScribe_adaptive Logs</h1>
  <div class="tabs">
    <div class="tab active" data-source="backend">Backend</div>
    <div class="tab" data-source="frontend">Frontend</div>
  </div>
  <input type="text" id="filter" placeholder="Filter (case-insensitive substring)...">
  <label><input type="checkbox" id="autorefresh" checked> Auto-refresh (3s)</label>
  <button id="refreshBtn">Refresh now</button>
  <a class="dl" id="downloadLink" href="/logs/backend/download" download>Download full log</a>
  <span id="status"></span>
</header>
<main><pre id="content">Loading...</pre></main>
<script>
let currentSource = "backend";
const contentEl = document.getElementById("content");
const statusEl = document.getElementById("status");
const filterEl = document.getElementById("filter");
const downloadLink = document.getElementById("downloadLink");

function setSource(source) {
  currentSource = source;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.source === source));
  downloadLink.href = "/logs/" + source + "/download";
  fetchLog();
}

document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => setSource(t.dataset.source)));
document.getElementById("refreshBtn").addEventListener("click", fetchLog);
filterEl.addEventListener("input", applyFilter);

function levelClass(line) {
  for (const lvl of ["CRITICAL", "ERROR", "WARNING", "INFO"]) {
    if (line.includes(" " + lvl + " ")) return "lvl-" + lvl;
  }
  return "";
}

function applyFilter() {
  const needle = filterEl.value.toLowerCase();
  contentEl.querySelectorAll(".line").forEach(el => {
    el.classList.toggle("hidden", needle && !el.textContent.toLowerCase().includes(needle));
  });
}

async function fetchLog() {
  statusEl.textContent = "Loading...";
  try {
    const res = await fetch("/logs/" + currentSource + "?lines=3000");
    const text = await res.text();
    const wasAtBottom = window.scrollY + window.innerHeight >= document.body.scrollHeight - 40;
    contentEl.innerHTML = "";
    text.split("\\n").forEach(line => {
      const span = document.createElement("span");
      span.className = "line " + levelClass(line);
      span.textContent = line + "\\n";
      contentEl.appendChild(span);
    });
    applyFilter();
    if (wasAtBottom) window.scrollTo(0, document.body.scrollHeight);
    statusEl.textContent = "Updated " + new Date().toLocaleTimeString();
  } catch (err) {
    statusEl.textContent = "Error: " + err;
  }
}

setInterval(() => {
  if (document.getElementById("autorefresh").checked) fetchLog();
}, 3000);

fetchLog();
</script>
</body>
</html>
"""

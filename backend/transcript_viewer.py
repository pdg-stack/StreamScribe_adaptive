"""A small web UI for the transcript history DB, served by this same
FastAPI app (already running on localhost:8000) -- mirrors log_viewer.py's
pattern exactly: the SQLite file lives on the frontend's host filesystem
(frontend/transcript_store.py, written natively outside Docker) and
reaches this container read-only via the same docker-compose bind mount
already used for the frontend log file -- both live in the same host
logs/frontend/ folder now, so it's one mount for both.

Opens its own short-lived, explicitly read-only connection per request
(mode=ro via a file: URI) rather than holding one open -- this container
must never attempt to write to a path it only has a read-only bind mount
for, and must never contend for a lock against the frontend's own writer
connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

DB_PATH = Path("/app/frontend_logs/transcripts.db")
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000

router = APIRouter(prefix="/transcripts", tags=["transcripts"])


def _connect() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/data")
def get_transcripts(q: str = "", limit: int = DEFAULT_LIMIT) -> JSONResponse:
    limit = max(1, min(limit, MAX_LIMIT))
    conn = _connect()
    if conn is None:
        return JSONResponse({"rows": [], "note": f"No transcript history yet at {DB_PATH} -- nothing has been finalized and saved yet."})
    try:
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT * FROM transcripts WHERE "
                "source_text LIKE ? OR dest_text LIKE ? OR source_lang LIKE ? OR dest_lang LIKE ? "
                "OR host LIKE ? OR model LIKE ? OR size LIKE ? OR source_app LIKE ? "
                "ORDER BY start_ts DESC LIMIT ?",
                (like, like, like, like, like, like, like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM transcripts ORDER BY start_ts DESC LIMIT ?", (limit,)).fetchall()
        return JSONResponse({"rows": [dict(r) for r in rows]})
    except sqlite3.Error as exc:
        return JSONResponse({"rows": [], "note": f"Could not read {DB_PATH}: {exc}"})
    finally:
        conn.close()


@router.get("", response_class=HTMLResponse)
def viewer_page() -> str:
    return _PAGE_HTML


_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>StreamScribe_adaptive -- Transcript History</title>
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
  input[type=text] {
    background: #2a2a2a; border: 1px solid #444; color: #eee; border-radius: 6px;
    padding: 6px 10px; font-size: 13px; min-width: 240px;
  }
  select {
    background: #2a2a2a; border: 1px solid #444; color: #eee; border-radius: 6px;
    padding: 5px 8px; font-size: 13px;
  }
  label { font-size: 13px; color: #bbb; display: flex; align-items: center; gap: 4px; }
  button {
    background: #333; color: #eee; border: 1px solid #444; border-radius: 6px;
    padding: 6px 12px; font-size: 13px; cursor: pointer;
  }
  button:hover { background: #3a3a3a; }
  nav.pages { display: flex; gap: 10px; font-size: 13px; }
  nav.pages a { color: #999; text-decoration: none; }
  nav.pages a.current { color: #fff; font-weight: 600; }
  nav.pages a:hover { color: #fff; }
  #status { font-size: 12px; color: #888; margin-left: auto; }
  main { flex: 1; overflow: auto; padding: 0; }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  th, td {
    text-align: left; padding: 7px 10px; border-bottom: 1px solid #2c2c2c;
    white-space: nowrap; max-width: 320px; overflow: hidden; text-overflow: ellipsis;
  }
  td.text-cell { white-space: normal; max-width: 420px; }
  th {
    position: sticky; top: 0; background: #242424; color: #bbb;
    font-weight: 600; border-bottom: 1px solid #3a3a3a;
  }
  tr:hover td { background: #232323; }
  tr.delayed td { color: #9a9a9a; font-style: italic; }
  .empty { padding: 24px; color: #888; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>Transcript History</h1>
  <nav class="pages">
    <a href="/logs">Logs</a>
    <a class="current" href="/transcripts">Transcripts</a>
    <a href="/stats">Stats</a>
  </nav>
  <input type="text" id="filter" placeholder="Search text, language, model...">
  <label>Rows <select id="limitSelect">
    <option value="200">200</option>
    <option value="500" selected>500</option>
    <option value="1000">1000</option>
    <option value="5000">5000</option>
  </select></label>
  <label><input type="checkbox" id="autorefresh" checked> Auto-refresh (5s)</label>
  <button id="refreshBtn">Refresh now</button>
  <span id="status"></span>
</header>
<main>
  <table id="tbl">
    <thead>
      <tr>
        <th>Start</th><th>End</th><th>Duration</th><th>Auto</th><th>Src</th><th>Dst</th>
        <th>Source text</th><th>Translation</th><th>Host</th><th>Model</th><th>Size</th><th>Source app</th>
      </tr>
    </thead>
    <tbody id="body"></tbody>
  </table>
  <div class="empty" id="empty" style="display:none"></div>
</main>
<script>
const bodyEl = document.getElementById("body");
const emptyEl = document.getElementById("empty");
const statusEl = document.getElementById("status");
const filterEl = document.getElementById("filter");
const limitEl = document.getElementById("limitSelect");
let debounceTimer = null;

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}
function fmtDuration(start, end) {
  const s = Math.max(0, end - start);
  return s < 1 ? (Math.round(s * 1000) + "ms") : (s.toFixed(1) + "s");
}
function esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}
// Shows the literal word "null" for a genuinely missing value (source_lang
// on a non-auto-detect row, source_app when nothing was detected, etc.)
// rather than a dash -- this view is a faithful look at the raw stored
// data, not a stylized display, so null should read as null.
function nullable(v) {
  return v == null ? "null" : esc(v);
}

async function fetchRows() {
  statusEl.textContent = "Loading...";
  try {
    const q = encodeURIComponent(filterEl.value);
    const limit = limitEl.value;
    const res = await fetch(`/transcripts/data?q=${q}&limit=${limit}`);
    const data = await res.json();
    bodyEl.innerHTML = "";
    if (!data.rows || data.rows.length === 0) {
      emptyEl.style.display = "block";
      emptyEl.textContent = data.note || "No transcript history yet.";
      document.getElementById("tbl").style.display = "none";
    } else {
      emptyEl.style.display = "none";
      document.getElementById("tbl").style.display = "table";
      for (const r of data.rows) {
        const tr = document.createElement("tr");
        if (r.is_delayed) tr.className = "delayed";
        tr.innerHTML = `
          <td>${esc(fmtTime(r.start_ts))}</td>
          <td>${esc(fmtTime(r.end_ts))}</td>
          <td>${esc(fmtDuration(r.start_ts, r.end_ts))}</td>
          <td>${r.auto_detect ? "yes" : "no"}</td>
          <td>${nullable(r.source_lang)}</td>
          <td>${nullable(r.dest_lang)}</td>
          <td class="text-cell">${esc(r.source_text)}</td>
          <td class="text-cell">${esc(r.dest_text)}</td>
          <td>${nullable(r.host)}</td>
          <td>${nullable(r.model)}</td>
          <td>${nullable(r.size)}</td>
          <td>${nullable(r.source_app)}</td>`;
        bodyEl.appendChild(tr);
      }
    }
    statusEl.textContent = "Updated " + new Date().toLocaleTimeString() + " -- " + data.rows.length + " row(s)";
  } catch (err) {
    statusEl.textContent = "Error: " + err;
  }
}

document.getElementById("refreshBtn").addEventListener("click", fetchRows);
limitEl.addEventListener("change", fetchRows);
filterEl.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(fetchRows, 300);
});

setInterval(() => {
  if (document.getElementById("autorefresh").checked) fetchRows();
}, 5000);

fetchRows();
</script>
</body>
</html>
"""

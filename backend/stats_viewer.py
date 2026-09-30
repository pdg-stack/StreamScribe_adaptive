"""Visualizations on top of the data this app already has -- no new
service, no external dependency: parses backend.log's own per-segment
result lines (main.py's "seq=... tier=... cpu=... elapsed=...s
queue_len=..." log) and aggregates the transcript history DB (already
served read-only by transcript_viewer.py) into small chart-ready JSON
payloads. Charts themselves are hand-drawn SVG in the page's own inline
JS (see _PAGE_HTML) -- consistent with this app's existing log_viewer.py/
transcript_viewer.py pages, which are also plain HTML/CSS/JS with zero
external script/style dependencies, so this still works with no internet
access and adds nothing to Docker's resource footprint.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from backend.logging_config import LOG_FILE as BACKEND_LOG_FILE
from backend.transcript_viewer import DB_PATH

router = APIRouter(prefix="/stats", tags=["stats"])

DEFAULT_MAX_POINTS = 2000

# Matches backend/main.py's _process_one_segment result log line, e.g.:
#   2026-09-30 08:47:11,838 INFO [streamscribe] seq=90 kind=final tier=tiny
#   cpu=red elapsed=2.82s queue_len=1 text='...'
# Timestamp format is logging's default asctime ("%Y-%m-%d %H:%M:%S,mmm"),
# set by logging_config.py's formatter.
_RESULT_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} \w+ \[\w+\] "
    r"seq=\d+ kind=(?P<kind>\w+) tier=(?P<tier>\S+) cpu=(?P<cpu>\w+) "
    r"elapsed=(?P<elapsed>[\d.]+)s queue_len=(?P<queue_len>\d+)"
)
_ERROR_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (ERROR|CRITICAL) ")


def _parse_backend_metrics(max_points: int = DEFAULT_MAX_POINTS) -> dict:
    if not BACKEND_LOG_FILE.exists():
        return {"points": [], "tier_counts": {}, "cpu_counts": {}, "error_count": 0}
    try:
        # Same "whole file fits in memory" assumption log_viewer.py's
        # _tail() already makes -- RotatingFileHandler caps this at 5MB.
        text = BACKEND_LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"points": [], "tier_counts": {}, "cpu_counts": {}, "error_count": 0}

    points: list[dict] = []
    tier_counts: Counter = Counter()
    cpu_counts: Counter = Counter()
    error_count = 0
    for line in text.splitlines():
        m = _RESULT_LINE_RE.match(line)
        if m:
            try:
                ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                continue
            tier_counts[m["tier"]] += 1
            cpu_counts[m["cpu"]] += 1
            points.append({
                "ts": ts, "kind": m["kind"], "tier": m["tier"], "cpu": m["cpu"],
                "elapsed": float(m["elapsed"]), "queue_len": int(m["queue_len"]),
            })
        elif _ERROR_LINE_RE.match(line):
            error_count += 1

    # Most-recent window only -- matches log_viewer.py's own tail-N
    # pattern; charting the full history adds nothing a recent window
    # doesn't already show, and keeps the JSON payload small.
    points = points[-max_points:]
    return {
        "points": points,
        "tier_counts": dict(tier_counts),
        "cpu_counts": dict(cpu_counts),
        "error_count": error_count,
    }


def _connect() -> sqlite3.Connection | None:
    if not Path(DB_PATH).exists():
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _duration_bucket_counts(conn: sqlite3.Connection) -> dict:
    buckets = {"<1s": 0, "1-3s": 0, "3-5s": 0, "5-10s": 0, ">10s": 0}
    rows = conn.execute("SELECT end_ts - start_ts AS dur FROM transcripts WHERE is_delayed = 0").fetchall()
    for row in rows:
        dur = max(0.0, row["dur"] or 0.0)
        if dur < 1:
            buckets["<1s"] += 1
        elif dur < 3:
            buckets["1-3s"] += 1
        elif dur < 5:
            buckets["3-5s"] += 1
        elif dur < 10:
            buckets["5-10s"] += 1
        else:
            buckets[">10s"] += 1
    return buckets


def _transcript_metrics() -> dict:
    empty = {
        "lang_counts": {}, "engine_counts": {}, "duration_buckets": {},
        "delayed_count": 0, "total_count": 0,
    }
    conn = _connect()
    if conn is None:
        return empty
    try:
        lang_rows = conn.execute(
            "SELECT COALESCE(source_lang, 'unknown') AS lang, COUNT(*) AS n FROM transcripts "
            "WHERE is_delayed = 0 GROUP BY lang ORDER BY n DESC LIMIT 12"
        ).fetchall()
        engine_rows = conn.execute(
            "SELECT COALESCE(host, '?') || ' / ' || COALESCE(model, '?') || ' / ' || COALESCE(size, '?') AS label, "
            "COUNT(*) AS n FROM transcripts WHERE is_delayed = 0 GROUP BY label ORDER BY n DESC LIMIT 12"
        ).fetchall()
        total_count = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
        delayed_count = conn.execute("SELECT COUNT(*) FROM transcripts WHERE is_delayed = 1").fetchone()[0]
        return {
            "lang_counts": {r["lang"]: r["n"] for r in lang_rows},
            "engine_counts": {r["label"]: r["n"] for r in engine_rows},
            "duration_buckets": _duration_bucket_counts(conn),
            "delayed_count": delayed_count,
            "total_count": total_count,
        }
    except sqlite3.Error:
        return empty
    finally:
        conn.close()


@router.get("/data")
def get_stats(max_points: int = DEFAULT_MAX_POINTS) -> JSONResponse:
    max_points = max(1, min(max_points, 20000))
    return JSONResponse({
        "backend": _parse_backend_metrics(max_points),
        "transcripts": _transcript_metrics(),
    })


@router.get("", response_class=HTMLResponse)
def viewer_page() -> str:
    return _PAGE_HTML


_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>StreamScribe_adaptive -- Stats</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0; background: #1a1a1a; color: #e8e8e8;
    font-family: "Segoe UI", system-ui, sans-serif;
  }
  header {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 16px; background: #242424; border-bottom: 1px solid #3a3a3a;
    flex-wrap: wrap; position: sticky; top: 0; z-index: 1;
  }
  h1 { font-size: 15px; font-weight: 600; margin: 0; margin-right: 8px; color: #fff; }
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
  main { padding: 16px; display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 16px; }
  .card { background: #222; border: 1px solid #333; border-radius: 10px; padding: 14px 16px; }
  .card h2 { font-size: 13px; font-weight: 600; margin: 0 0 10px; color: #cfe8ff; }
  .card.wide { grid-column: 1 / -1; }
  .stat-row { display: flex; gap: 24px; flex-wrap: wrap; }
  .stat { min-width: 120px; }
  .stat .n { font-size: 26px; font-weight: 700; color: #fff; }
  .stat .lbl { font-size: 12px; color: #999; }
  .empty { color: #888; font-size: 13px; padding: 8px 0; }
  svg { width: 100%; height: auto; display: block; }
  .bar-label { font-size: 10.5px; fill: #bbb; }
  .bar-value { font-size: 10.5px; fill: #eee; }
  .axis { stroke: #3a3a3a; stroke-width: 1; }
</style>
</head>
<body>
<header>
  <h1>StreamScribe_adaptive Stats</h1>
  <nav class="pages">
    <a href="/logs">Logs</a>
    <a href="/transcripts">Transcripts</a>
    <a class="current" href="/stats">Stats</a>
  </nav>
  <label><input type="checkbox" id="autorefresh" checked> Auto-refresh (10s)</label>
  <button id="refreshBtn">Refresh now</button>
  <span id="status"></span>
</header>
<main id="main">Loading...</main>
<script>
const COLORS = { green: "#3fbf50", yellow: "#e0b400", red: "#e0392b", accent: "#7fbfff", bar: "#4a90d9" };
const mainEl = document.getElementById("main");
const statusEl = document.getElementById("status");

function svg(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

function card(title, wide) {
  const div = document.createElement("div");
  div.className = "card" + (wide ? " wide" : "");
  const h = document.createElement("h2");
  h.textContent = title;
  div.appendChild(h);
  return div;
}

// Simple vertical bar chart: entries = [[label, value, color?], ...]
function barChart(entries, opts) {
  opts = opts || {};
  const width = 600, barH = 20, gap = 8, leftPad = 140, rightPad = 50, topPad = 6;
  const height = topPad * 2 + entries.length * (barH + gap);
  const maxVal = Math.max(1, ...entries.map(e => e[1]));
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "xMinYMin meet" });
  entries.forEach(([label, value, color], i) => {
    const y = topPad + i * (barH + gap);
    const w = Math.round((value / maxVal) * (width - leftPad - rightPad));
    const t = svg("text", { x: leftPad - 8, y: y + barH * 0.7, "text-anchor": "end", class: "bar-label" });
    t.textContent = label;
    root.appendChild(t);
    root.appendChild(svg("rect", { x: leftPad, y, width: Math.max(w, 2), height: barH, rx: 3, fill: color || COLORS.bar }));
    const vt = svg("text", { x: leftPad + w + 6, y: y + barH * 0.7, class: "bar-value" });
    vt.textContent = value;
    root.appendChild(vt);
  });
  return root;
}

// Simple line chart over time: points = [{ts, value}, ...]
function lineChart(points, opts) {
  opts = opts || {};
  const width = 600, height = 160, pad = 28;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "xMinYMin meet" });
  if (points.length === 0) return root;
  const xs = points.map(p => p.ts), ys = points.map(p => p.value);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const maxY = Math.max(opts.minMax || 0, ...ys);
  const sx = v => pad + (maxX === minX ? 0 : (v - minX) / (maxX - minX) * (width - pad * 1.5));
  const sy = v => height - pad - (maxY === 0 ? 0 : v / maxY * (height - pad * 1.5));
  root.appendChild(svg("line", { x1: pad, y1: height - pad, x2: width - 8, y2: height - pad, class: "axis" }));
  root.appendChild(svg("line", { x1: pad, y1: 8, x2: pad, y2: height - pad, class: "axis" }));
  const d = points.map((p, i) => `${i === 0 ? "M" : "L"} ${sx(p.ts).toFixed(1)} ${sy(p.value).toFixed(1)}`).join(" ");
  root.appendChild(svg("path", { d, fill: "none", stroke: opts.color || COLORS.accent, "stroke-width": 1.5 }));
  if (opts.threshold) {
    const ty = sy(opts.threshold);
    root.appendChild(svg("line", { x1: pad, y1: ty, x2: width - 8, y2: ty, stroke: COLORS.red, "stroke-width": 1, "stroke-dasharray": "4,3" }));
  }
  const maxLabel = svg("text", { x: 4, y: 14, class: "bar-value" });
  maxLabel.textContent = (opts.yFmt || (v => v.toFixed(1)))(maxY);
  root.appendChild(maxLabel);
  return root;
}

function statBlock(items) {
  const row = document.createElement("div");
  row.className = "stat-row";
  items.forEach(([n, lbl]) => {
    const s = document.createElement("div");
    s.className = "stat";
    s.innerHTML = `<div class="n">${n}</div><div class="lbl">${lbl}</div>`;
    row.appendChild(s);
  });
  return row;
}

function emptyNote(text) {
  const p = document.createElement("div");
  p.className = "empty";
  p.textContent = text;
  return p;
}

async function refresh() {
  statusEl.textContent = "Loading...";
  try {
    const res = await fetch("/stats/data");
    const data = await res.json();
    render(data);
    statusEl.textContent = "Updated " + new Date().toLocaleTimeString();
  } catch (err) {
    statusEl.textContent = "Error: " + err;
  }
}

function render(data) {
  mainEl.innerHTML = "";
  const b = data.backend, t = data.transcripts;

  const overview = card("Overview", true);
  overview.appendChild(statBlock([
    [t.total_count, "transcript entries"],
    [t.delayed_count, "delayed markers" + (t.total_count ? ` (${(100 * t.delayed_count / t.total_count).toFixed(1)}%)` : "")],
    [b.points.length, "ASR segments (recent window)"],
    [b.error_count, "processor errors (recent window)"],
  ]));
  mainEl.appendChild(overview);

  const elapsedCard = card("Segment processing time (elapsed, seconds) over time");
  if (b.points.length) {
    elapsedCard.appendChild(lineChart(b.points.map(p => ({ ts: p.ts, value: p.elapsed })), { color: COLORS.accent, yFmt: v => v.toFixed(1) + "s" }));
  } else {
    elapsedCard.appendChild(emptyNote("No ASR segments in the current backend.log window yet."));
  }
  mainEl.appendChild(elapsedCard);

  const queueCard = card("Queue length over time");
  if (b.points.length) {
    queueCard.appendChild(lineChart(b.points.map(p => ({ ts: p.ts, value: p.queue_len })), { color: "#e08f3f", yFmt: v => Math.round(v) }));
  } else {
    queueCard.appendChild(emptyNote("No ASR segments in the current backend.log window yet."));
  }
  mainEl.appendChild(queueCard);

  const cpuCard = card("CPU strain distribution");
  const cpuEntries = ["green", "yellow", "red"].filter(k => b.cpu_counts[k]).map(k => [k, b.cpu_counts[k], COLORS[k]]);
  cpuCard.appendChild(cpuEntries.length ? barChart(cpuEntries) : emptyNote("No data yet."));
  mainEl.appendChild(cpuCard);

  const tierCard = card("Active tier/engine distribution");
  const tierEntries = Object.entries(b.tier_counts).sort((a, c) => c[1] - a[1]);
  tierCard.appendChild(tierEntries.length ? barChart(tierEntries) : emptyNote("No data yet."));
  mainEl.appendChild(tierCard);

  const langCard = card("Transcript entries by source language");
  const langEntries = Object.entries(t.lang_counts).sort((a, c) => c[1] - a[1]);
  langCard.appendChild(langEntries.length ? barChart(langEntries) : emptyNote("No transcript history yet."));
  mainEl.appendChild(langCard);

  const engineCard = card("Transcript entries by host / model / size");
  const engineEntries = Object.entries(t.engine_counts).sort((a, c) => c[1] - a[1]);
  engineCard.appendChild(engineEntries.length ? barChart(engineEntries) : emptyNote("No transcript history yet."));
  mainEl.appendChild(engineCard);

  const durCard = card("Segment duration distribution", true);
  const durEntries = Object.entries(t.duration_buckets);
  const durTotal = durEntries.reduce((s, [, v]) => s + v, 0);
  durCard.appendChild(durTotal ? barChart(durEntries) : emptyNote("No transcript history yet."));
  mainEl.appendChild(durCard);
}

document.getElementById("refreshBtn").addEventListener("click", refresh);
setInterval(() => { if (document.getElementById("autorefresh").checked) refresh(); }, 10000);
refresh();
</script>
</body>
</html>
"""

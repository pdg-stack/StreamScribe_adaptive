"""Persists finalized caption entries (and <delayed> markers) to a local
SQLite database, so there's a searchable history of everything transcribed
-- otherwise lost the moment persist_subtitles/replace-mode clears the
on-screen transcript, or the app closes.

"Lazy" writing (per request): a row is only committed DEBOUNCE_S after its
entry is marked final (or a <delayed> marker is shown), not the instant it
arrives. A "final" kind is, by the backend's own segmentation contract
(vad_segmenter.py), genuinely finished -- no further partial will ever
refine it -- so this isn't waiting for more text to arrive; it's a
deliberate settle margin so a stale/duplicate late event for the same
utterance can't land two rows for one spoken sentence.

journal_mode=DELETE (SQLite's traditional rollback journal), not the
default-in-modern-SQLite WAL mode: WAL relies on a shared-memory file
(-wal/-shm) between reader and writer, which doesn't reliably cross the
container/host filesystem boundary the way a plain journal file does --
backend/transcript_viewer.py reads this same file read-only from inside
Docker over a bind mount (see docker-compose.yml), the same pattern
already used for the frontend log file.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .logging_config import log

# Its own subdirectory, not directly under frontend/ -- docker-compose.yml
# bind-mounts this directory (not the single .db file) read-only into the
# backend container, mirroring the frontend/logs mount that already works
# reliably: bind-mounting a single file that doesn't exist yet is a known
# Docker gotcha (it silently creates a directory at that path instead),
# and this file doesn't exist until the frontend runs for the first time.
DB_DIR = Path(__file__).parent / "data"
DB_PATH = DB_DIR / "transcripts.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    is_delayed INTEGER NOT NULL DEFAULT 0,
    auto_detect INTEGER NOT NULL DEFAULT 0,
    source_lang TEXT,
    dest_lang TEXT,
    source_text TEXT NOT NULL DEFAULT '',
    dest_text TEXT NOT NULL DEFAULT '',
    host TEXT,
    model TEXT,
    size TEXT,
    source_app TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_start_ts ON transcripts(start_ts);
"""


class TranscriptStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def insert(
        self, *, start_ts: float, end_ts: float, is_delayed: bool, auto_detect: bool,
        source_lang: str | None, dest_lang: str | None, source_text: str, dest_text: str,
        host: str, model: str, size: str, source_app: str | None,
    ) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO transcripts "
                    "(start_ts, end_ts, is_delayed, auto_detect, source_lang, dest_lang, "
                    "source_text, dest_text, host, model, size, source_app, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        start_ts, end_ts, int(is_delayed), int(auto_detect), source_lang, dest_lang,
                        source_text, dest_text, host, model, size, source_app, time.time(),
                    ),
                )
                self._conn.commit()
        except sqlite3.Error:
            # A transcript-history write failing must never take down live
            # transcription -- this is a best-effort record, not something
            # the rest of the app depends on.
            log.exception("Failed to write transcript row to %s", DB_PATH)

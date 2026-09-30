"""Structured, persistent logging for post-hoc analysis -- the console
window launch.bat opens is closed (and its scrollback lost) the instant
the app exits or the window is closed, so nothing printed there survives
past that session. Every log record also goes to a rotating file under
the repo's top-level logs/ folder, so a session's behavior can be
inspected after the fact instead of only while the console is still open.

Everything under logs/ -- this file, the backend's own log, and the
transcript history DB (transcript_store.py) -- lives in that one
gitignored tree, not scattered under frontend/ and backend/ separately.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs" / "frontend"
LOG_FILE = LOG_DIR / "frontend.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

_configured = False


def setup_logging() -> logging.Logger:
    global _configured
    logger = logging.getLogger("streamscribe")
    if _configured:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    # Console: unchanged in spirit from the previous plain print() calls
    # (still shows live in the console launch.bat opens), just routed
    # through the same logger as the file handler below.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("Could not open %s for file logging: %s", LOG_FILE, exc)

    logger.propagate = False
    _configured = True
    return logger


log = setup_logging()

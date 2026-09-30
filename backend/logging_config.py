"""Structured, persistent logging for post-hoc analysis -- stdout alone
(what `docker logs` shows) is lost once the container is recreated, and
doesn't survive being scrolled past during a long session. Every log
record also goes to a rotating file under LOG_DIR, so a session's
behavior can be inspected after the fact instead of only live.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))
LOG_FILE = LOG_DIR / "backend.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

_configured = False


def setup_logging() -> logging.Logger:
    global _configured
    logger = logging.getLogger("streamscribe")
    if _configured:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:
        # Don't let an unwritable log directory take the whole backend
        # down -- stdout logging (already added above) still works either
        # way, this is a best-effort persistence layer on top of it.
        logger.warning("Could not open %s for file logging: %s", LOG_FILE, exc)

    logger.propagate = False
    _configured = True
    return logger


log = setup_logging()

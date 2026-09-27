"""Settings model + persistence, mirroring LocalScribe_whisper_modal's
user_prefs.json pattern: font/color/opacity/refresh-speed for the overlay,
persisted to a JSON file next to this app so it survives restarts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

PREFS_PATH = Path(__file__).parent / "user_prefs.json"


@dataclass
class Settings:
    font_family: str = "Segoe UI"
    font_size: int = 15
    font_color: str = "#FFFFFF"
    background_color: str = "#000000"
    background_opacity: int = 60  # 0-100
    refresh_speed_ms: int = 400  # min interval between partial-text UI updates
    src_language: str = "auto"
    dest_language: str = "en"
    window_x: int | None = None
    window_y: int | None = None

    @classmethod
    def load(cls) -> "Settings":
        if PREFS_PATH.exists():
            try:
                data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
                return cls(**{**asdict(cls()), **data})
            except (json.JSONDecodeError, TypeError):
                pass
        return cls()

    def save(self) -> None:
        PREFS_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

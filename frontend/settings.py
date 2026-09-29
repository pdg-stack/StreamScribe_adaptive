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
    engine: str = "faster-whisper"  # "faster-whisper" | "parakeet"
    tier: str = "auto"  # "auto" | "small" | "base" | "tiny" -- faster-whisper only
    acceptable_latency_s: float = 1.0  # 1-20s, see plan
    advanced_mode: bool = False
    inference_mode: str = "local"  # "local" | "modal"
    persist_subtitles: bool = False  # False: newest caption replaces the last; True: appends, scrollable
    auto_hide_header: bool = False  # hide the toolbar unless the mouse is over the overlay
    auto_hide_footer: bool = False  # hide the advanced pane unless the mouse is over the overlay
    border_color: str = "#444444"
    border_thickness: int = 0  # px; 0 = no border

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

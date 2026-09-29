"""Settings model + persistence, mirroring LocalScribe_whisper_modal's
user_prefs.json pattern: font/color/opacity/refresh-speed for the overlay,
persisted to a JSON file next to this app so it survives restarts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
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
    # The caption text's own outline -- a stroke that follows the actual
    # shape of the glyphs (see caption_view.py), like a font's own outline,
    # not a border drawn around a bounding box.
    outline_color: str = "#444444"
    outline_width: int = 0  # px; 0 = no outline
    # A highlight rectangle sized to each caption line, independent of
    # both the outline above and background_color/opacity below (the app
    # window's own panel translucency) -- e.g. YouTube's per-line caption
    # background. Colors may carry alpha (#AARRGGBB) from the color
    # picker's transparency option; 0 opacity here means no box at all.
    font_background_color: str = "#000000"
    font_background_opacity: int = 0  # 0-100; 0 = no background box
    # Recently used colors (hex, #RRGGBB or #AARRGGBB), most-recent first
    # -- restores QColorDialog's custom-color swatches across restarts
    # (its own built-in memory is process-lifetime only).
    custom_colors: list[str] = field(default_factory=list)
    # Modal auth: optional, only needed if the backend container has no
    # ambient `modal token set` credentials of its own (the normal case --
    # Docker doesn't inherit the host's ~/.modal.toml). Left blank, Modal
    # setup falls back to whatever ambient auth the container happens to
    # have. Get these from modal.com -> Settings -> API Tokens. Stored
    # in this plaintext local prefs file like every other setting here --
    # there's no secrets vault in this app, so treat this file as
    # sensitive once a token's been entered.
    modal_token_id: str = ""
    modal_token_secret: str = ""

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

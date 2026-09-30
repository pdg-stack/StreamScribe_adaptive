"""Active audio-source labeling via pycaw session peak-metering.
Process-level only: Windows' audio session APIs report which *process* is
making sound, not which browser tab -- see the plan's Source-aware capture
section. Since only one source is typically active at a time (per the
product requirement), simple peak-detection is sufficient -- no need for
true per-app audio isolation.
"""

from __future__ import annotations

import os

import win32api
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

_PEAK_THRESHOLD = 0.01
_OWN_PID = os.getpid()

# Keyed by exe path, not process name: two different apps can share an exe
# name (rare) but never a path, and this avoids re-reading the same file's
# version info on every ~1s poll.
_name_cache: dict[str, str] = {}


def _resolve_friendly_name(exe_path: str) -> str | None:
    """The exe's own FileDescription version-info field -- e.g. "Google
    Chrome" for chrome.exe, "Notepad" for notepad.exe -- rather than the
    raw process/executable name. Returns None if the exe has no version
    resource (some apps genuinely don't ship one) or it can't be read."""
    try:
        translations = win32api.GetFileVersionInfo(exe_path, "\\VarFileInfo\\Translation")
        lang, codepage = translations[0]
        info_path = "\\StringFileInfo\\%04x%04x\\FileDescription" % (lang, codepage)
        description = win32api.GetFileVersionInfo(exe_path, info_path)
        return description.strip() or None
    except Exception:
        return None


def _fallback_name(process_name: str) -> str:
    """Used only when FileDescription isn't available -- still better than
    the raw "chrome.exe"/"python.exe" process name."""
    stem = process_name[:-4] if process_name.lower().endswith(".exe") else process_name
    return stem.replace("_", " ").replace("-", " ").title()


def _friendly_name(process) -> str:
    try:
        exe_path = process.exe()
    except Exception:
        exe_path = ""

    if not exe_path:
        return _fallback_name(process.name())
    if exe_path not in _name_cache:
        _name_cache[exe_path] = _resolve_friendly_name(exe_path) or _fallback_name(process.name())
    return _name_cache[exe_path]


def active_source_process() -> str | None:
    """Returns the friendly application name (not the raw process/exe
    name) of the audio session with the highest current peak level, or
    None if nothing is audibly playing. Cheap enough to poll periodically
    (e.g. once per second) from the main loop -- no need to subscribe to
    session-change events for a single-user local app."""
    best_name: str | None = None
    best_peak = 0.0

    for session in AudioUtilities.GetAllSessions():
        if session.Process is None:
            continue  # system sounds session, not an app
        if session.Process.pid == _OWN_PID:
            # This app's own WASAPI *loopback capture* client shows up in
            # GetAllSessions() alongside real playback sessions (it's
            # opened against the same render endpoint it's tapping), and
            # its peak meter mirrors whatever it's capturing -- so without
            # this check, this app can end up "detecting" itself as the
            # source of the very audio it's only listening in on.
            continue
        try:
            meter = session._ctl.QueryInterface(IAudioMeterInformation)
            peak = meter.GetPeakValue()
        except Exception:
            continue
        if peak > best_peak:
            best_peak = peak
            best_name = _friendly_name(session.Process)

    return best_name if best_peak > _PEAK_THRESHOLD else None

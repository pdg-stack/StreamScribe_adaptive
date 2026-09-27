"""Active audio-source labeling via pycaw session peak-metering.
Process-level only: Windows' audio session APIs report which *process* is
making sound, not which browser tab -- see the plan's Source-aware capture
section. Reports e.g. "chrome.exe", not a page title. Since only one
source is typically active at a time (per the product requirement), simple
peak-detection is sufficient -- no need for true per-app audio isolation.
"""

from __future__ import annotations

from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

_PEAK_THRESHOLD = 0.01


def active_source_process() -> str | None:
    """Returns the process name of the audio session with the highest
    current peak level, or None if nothing is audibly playing. Cheap
    enough to poll periodically (e.g. once per second) from the main
    loop -- no need to subscribe to session-change events for a
    single-user local app."""
    best_name: str | None = None
    best_peak = 0.0

    for session in AudioUtilities.GetAllSessions():
        if session.Process is None:
            continue  # system sounds session, not an app
        try:
            meter = session._ctl.QueryInterface(IAudioMeterInformation)
            peak = meter.GetPeakValue()
        except Exception:
            continue
        if peak > best_peak:
            best_peak = peak
            best_name = session.Process.name()

    return best_name if best_peak > _PEAK_THRESHOLD else None

"""WASAPI loopback capture: records whatever Windows is currently
outputting (any app, not the microphone) and resamples to 16kHz mono
int16 PCM for the backend. pyaudiowpatch was chosen over `soundcard` for
its purpose-built, more battle-tested WASAPI loopback support (see plan).

PLAIN-ENGLISH OVERVIEW (for anyone new to this file):
"Loopback" capture means recording Windows' own audio OUTPUT (whatever
you'd hear through your speakers/headphones) rather than a microphone
INPUT -- that's how this app can caption a YouTube video or any other
app's sound without needing you to speak into anything.
The `callback` function inside start() below is the heart of this file:
Windows calls it automatically, over and over, a little chunk of audio at
a time (about 1024 samples each), for as long as the stream is running.
Each time it's called, this code: converts that chunk to the format the
backend expects (16kHz, mono, 16-bit), hands it off to be sent over the
network, and does a quick "is this actually sound or just silence?" check
to drive the on-screen status light. Everything happens inside that one
function -- there's no separate loop you need to go looking for.
"""

from __future__ import annotations

from collections.abc import Callable
from math import gcd

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly

from .logging_config import log

TARGET_SAMPLE_RATE = 16000
CHUNK_FRAMES = 1024

# Rough "is there real sound right now" gate for the overlay's status light --
# not a VAD (the backend's webrtcvad is the real authority on speech), just a
# cheap amplitude check so the light can react to audio the instant it starts
# rather than waiting several seconds for a backend round-trip. High enough
# to not be tripped by a loopback device's idle noise floor (observed to get
# the light stuck "on" during genuine silence at very low thresholds); the
# overlay's own 5s listening-timeout (see OverlayWindow.pulse_listening) is
# the second, independent line of defense against that.
ACTIVITY_AMPLITUDE_THRESHOLD = 150


class LoopbackCapture:
    """Captures a loopback device's stream and invokes `on_audio(pcm_bytes)`
    with resampled 16kHz mono int16 PCM for each chunk, plus `on_activity()`
    (no args) whenever a chunk looks like real sound rather than silence.
    Both callbacks run on PortAudio's own internal thread, not the Qt main
    thread."""

    def __init__(self, on_audio: Callable[[bytes], None], on_activity: Callable[[], None] | None = None) -> None:
        self._on_audio = on_audio
        self._on_activity = on_activity
        self._pa = pyaudio.PyAudio()
        self._stream = None
        self.device: dict | None = None

    def default_loopback_device(self) -> dict:
        """The loopback counterpart of the current default output device --
        what actually gets captured when nothing else is selected."""
        wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = self._pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        if not default_speakers.get("isLoopbackDevice"):
            for device in self._pa.get_loopback_device_info_generator():
                if default_speakers["name"] in device["name"]:
                    return device
            raise RuntimeError(f"No loopback device found for default output {default_speakers['name']!r}")
        return default_speakers

    def loopback_devices(self) -> list[dict]:
        """All available loopback devices, for a device picker -- needed
        when audio moves between speakers/headphones/HDMI outputs."""
        return list(self._pa.get_loopback_device_info_generator())

    def start(self, device: dict | None = None) -> None:
        self.device = device or self.default_loopback_device()
        channels = int(self.device["maxInputChannels"])
        rate = int(self.device["defaultSampleRate"])
        g = gcd(TARGET_SAMPLE_RATE, rate)
        up, down = TARGET_SAMPLE_RATE // g, rate // g

        def callback(in_data, frame_count, time_info, status):
            # This runs on PortAudio's own C-spawned thread, not a Python
            # `threading.Thread` -- an uncaught exception here does NOT reach
            # sys.excepthook/threading.excepthook (main.py's handlers only
            # ever cover real Python threads), and PortAudio's ctypes callback
            # wrapper is known to just stop invoking a callback that raised,
            # with nothing surfacing anywhere but a stderr traceback that
            # vanishes with the console. This was the prime suspect for a
            # session where only the first audio segment was ever
            # transcribed and nothing else showed up again -- unconfirmed
            # from this one log alone, but every other path here already had
            # this exact bug pattern this round, and this callback had zero
            # protection. Every branch below must stay inside this try/except
            # and keep returning paContinue, or one bad frame permanently
            # kills audio input until the app is restarted.
            try:
                if status:
                    log.warning("PortAudio callback status flag: %r", status)
                if not in_data:
                    return (None, pyaudio.paContinue)
                audio = np.frombuffer(in_data, dtype=np.float32)
                if channels > 1:
                    audio = audio.reshape(-1, channels).mean(axis=1)
                resampled = resample_poly(audio, up, down)
                pcm16 = np.clip(resampled * 32768.0, -32768, 32767).astype(np.int16)
                self._on_audio(pcm16.tobytes())
                if self._on_activity is not None and np.abs(pcm16).mean() > ACTIVITY_AMPLITUDE_THRESHOLD:
                    self._on_activity()
            except Exception:
                log.exception("PortAudio capture callback failed on one chunk -- continuing")
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=self.device["index"],
            frames_per_buffer=CHUNK_FRAMES,
            stream_callback=callback,
        )
        self._stream.start_stream()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
        self._pa.terminate()

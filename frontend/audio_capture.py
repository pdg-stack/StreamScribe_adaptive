"""WASAPI loopback capture: records whatever Windows is currently
outputting (any app, not the microphone) and resamples to 16kHz mono
int16 PCM for the backend. pyaudiowpatch was chosen over `soundcard` for
its purpose-built, more battle-tested WASAPI loopback support (see plan).
"""

from __future__ import annotations

from collections.abc import Callable
from math import gcd

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly

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
            audio = np.frombuffer(in_data, dtype=np.float32)
            if channels > 1:
                audio = audio.reshape(-1, channels).mean(axis=1)
            resampled = resample_poly(audio, up, down)
            pcm16 = np.clip(resampled * 32768.0, -32768, 32767).astype(np.int16)
            self._on_audio(pcm16.tobytes())
            if self._on_activity is not None and np.abs(pcm16).mean() > ACTIVITY_AMPLITUDE_THRESHOLD:
                self._on_activity()
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

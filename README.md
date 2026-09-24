# StreamScribe_fwhisper

A small floating, transparent, always-on-top window that live-transcribes and translates whatever audio your Windows machine is currently outputting — a browser tab, VLC, Gemini voice, an MP3 player — not your microphone.

## Architecture

This app is split across two runtimes, and that split is intentional, not accidental:

- **`backend/`** — a Dockerized FastAPI service. Does the actual work: speech segmentation (`webrtcvad`), speech-to-text (`faster-whisper`, CPU-only, auto-degrading `small` → `base` → `tiny` under load), and translation (`NLLB-200-distilled-600M`, all languages, auto-detected source, English destination by default). Runs entirely offline once its models are cached.
- **`frontend/`** — a native Windows Python process (PyQt6) that runs **outside Docker**. It captures system-audio loopback (`pyaudiowpatch`) and renders the floating overlay. This has to run outside Docker: containers on Windows have no access to host audio devices (WASAPI) or the desktop compositor, so the overlay and audio capture cannot be containerized.

The two talk over a single localhost WebSocket (`ws://127.0.0.1:8000/ws/transcribe`) — raw audio one way, transcript/translation/status JSON the other way.

**"Runs locally, Docker-hosted" refers to the backend only.** The frontend is a native script/executable you run directly on Windows.

## Setup

```bash
# 1. Start the backend (first run downloads models into a cached volume)
docker compose up -d

# 2. Start the frontend (from the frontend/ directory, or via the Desktop launcher)
pip install -r native-requirements.txt
python frontend/main.py
```

A ready-to-use `launch.bat` is also placed on the Desktop, which checks the backend is reachable before starting the overlay.

## License

MIT — see [LICENSE](LICENSE).

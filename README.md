# StreamScribe_adaptive

A small floating, transparent, always-on-top window that live-transcribes and translates whatever audio your Windows machine is currently outputting — a browser tab, VLC, Gemini voice, an MP3 player — not your microphone.

## Architecture

This app is split across two runtimes, and that split is intentional, not accidental:

- **`backend/`** — a Dockerized FastAPI service. Segments speech (`webrtcvad`), transcribes it via one of three interchangeable engines (`faster-whisper`, CPU-only, `Auto`-degrading `small` → `base` → `tiny` under load; **Parakeet TDT** for ~4x the CPU speed on a narrower set of languages; or opt-in **Modal** cloud GPU), and translates it (`NLLB-200-distilled-600M`, auto-detected source where the engine supports it, English destination by default). Runs entirely offline unless you explicitly switch to Modal.
- **`frontend/`** — a native Windows Python process (PyQt6) that runs **outside Docker**. It captures system-audio loopback (`pyaudiowpatch`) and renders the floating overlay. This has to run outside Docker: containers on Windows have no access to host audio devices (WASAPI) or the desktop compositor, so the overlay and audio capture cannot be containerized.

The two talk over a single localhost WebSocket (`ws://127.0.0.1:8000/ws/transcribe`) — raw audio one way, transcript/translation/status JSON the other way. See [`docs/PRD.md`](docs/PRD.md) for the full pipeline and wire protocol, and [`docs/architecture.html`](docs/architecture.html) for the labeled component diagram.

**"Runs locally, Docker-hosted" refers to the backend only.** The frontend is a native script/executable you run directly on Windows. Modal is an explicit, opt-in exception to "offline by default" — see the PRD.

## Setup

```bash
# 1. Start the backend (first run downloads models into a cached volume)
docker compose up -d

# 2. Start the frontend (from the repo root, or via the Desktop launcher)
pip install -r native-requirements.txt
python -m frontend.main
```

A ready-to-use `launch.bat` is also placed on the Desktop, which checks the backend is reachable before starting the overlay.

## License

MIT — see [LICENSE](LICENSE).

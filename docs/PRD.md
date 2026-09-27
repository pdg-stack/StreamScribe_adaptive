# StreamScribe_adaptive — PRD

## Overview

A Windows desktop overlay that live-transcribes and translates whatever audio the OS is currently outputting — a browser tab, VLC, Gemini voice, an MP3 player — into a small, transparent, always-on-top floating window. It captures system audio only (never the microphone), runs fully offline by default, and does not require the audio's source app to cooperate in any way.

## Users & scope

Single user, single machine, personal use. No multi-user auth, no accounts, no telemetry. The only "deployment" is this one Windows laptop; there is no hosted service.

## Architecture

Two runtimes that must stay separate, because Docker on Windows cannot reach host audio devices or the desktop compositor:

- **`frontend/`** — a native Windows Python process (PyQt6), running directly on the host, outside Docker. Captures system-audio loopback via WASAPI (`pyaudiowpatch`), resamples to 16kHz mono, streams it to the backend over a WebSocket, and renders the floating overlay.
- **`backend/`** — a Dockerized FastAPI service exposing a single `/ws/transcribe` WebSocket. Segments incoming audio (VAD), transcribes each segment, translates it, and streams JSON events back.

```
┌─────────────────────────────┐        ws://127.0.0.1:8000/ws/transcribe        ┌──────────────────────────────────────────┐
│  frontend/ (native Windows)  │  binary PCM (16kHz mono) ───────────────────▶  │  backend/ (Docker container)              │
│                              │                                                 │                                            │
│  WASAPI loopback capture     │  ◀─────────────────────── JSON events          │  VadSegmenter → SegmentQueue → engine     │
│  pycaw source labeling       │                                                 │  (faster-whisper | Parakeet TDT | Modal)  │
│  PyQt6 overlay window        │                                                 │  → NLLB-200 translation                   │
└─────────────────────────────┘                                                 └──────────────────────────────────────────┘
```

See [`docs/architecture.html`](architecture.html) for the full labeled diagram — every component (WASAPI capture, pycaw labeling, VadSegmenter, SegmentQueue, each ASR engine, the NLLB translator, and Modal's GPU container + shared model-cache volume) shown individually, with the exact wire messages on each connection.

### Backend pipeline, per audio segment

1. **VAD segmentation** (`transcription/vad_segmenter.py`) — `webrtcvad` classifies 30ms frames as speech/silence, buffers a spoken utterance, and closes it into a bounded segment after ~500ms of trailing silence (or a 10s max-length cutoff). Also emits periodic "partial" segments on long utterances so the overlay doesn't look frozen mid-sentence.
2. **Queueing** (`transcription/segment_queue.py`) — closed segments go onto a `SegmentQueue`, decoupling "receive audio" from "process it" as two concurrent asyncio tasks, so a slow segment never blocks reading more audio off the socket.
3. **Queue preemption** — the queue tracks a rolling average of actual processing time. If the predicted wait for the newest queued segment exceeds 1.2× the user's acceptable-latency setting, every older queued segment is dropped so the engine catches up to "now" instead of grinding through stale backlog.
4. **Transcription** — one of three interchangeable engines, selected per connection:
   - **`faster-whisper`** (CTranslate2, int8) — tiny/base/small tiers, or `Auto` (starts at `small`, steps down under sustained RTF strain, steps back up when it clears). The only engine covering all required languages.
   - **Parakeet TDT 0.6B v3** (NVIDIA, via sherpa-onnx's quantized ONNX export) — ~4x faster than `small` on this CPU, but covers only Russian/Spanish/English/Italian/Portuguese, and doesn't report which language it detected.
   - **Modal (opt-in cloud GPU)** — `faster-whisper` only, deployed on demand to a Modal.com GPU function (reusing the sibling LocalScribe_whisper_modal project's model-cache Volume), never kept continuously warm.
5. **Translation** (`translation/translator.py`) — NLLB-200-distilled-600M (CTranslate2, int8), source language auto-detected by the ASR step (faster-whisper only — Parakeet doesn't expose this), destination user-selected.
6. **Response** — a JSON event carries the transcript, translation, active model tier, a `cpu_status` traffic-light color, the segment's close timestamp, and current queue length.

### Frontend

- WASAPI loopback capture, resampled and streamed to the backend as it's captured.
- `pycaw` session peak-metering labels which process is currently the loudest audio source (process-level only — see Risks for the audio-enhancer caveat).
- A transparent, always-on-top, draggable/resizable overlay: a toolbar (source language, swap, destination language, settings, close) above a caption panel. An **advanced pane**, toggled in Settings, surfaces diagnostic detail (queue length, acceptable-delay value, active model/size, local vs. cloud) that the simple view intentionally hides.
- Settings: font/color/background-opacity/refresh-speed (persisted), engine + tier selection, acceptable-latency spinner, local/Modal toggle with setup/status/stop controls, window position (persisted).

## Functional requirements

- Transcribe and translate live system audio in near-real-time, with no dependency on the source application.
- Support English, Hindi (dropped as a requirement — see Non-goals), Chinese, Russian, Spanish, Italian, Portuguese, Japanese depending on engine.
- Degrade gracefully under CPU strain (`Auto` tier) rather than falling further and further behind.
- Never display a caption staler than the user's configured acceptable-latency threshold — show `<delayed>` and move on instead.
- Let the user choose CPU-local or opt-in cloud (Modal) inference, never both silently.

## Non-functional requirements

- Fully offline by default; no network dependency unless the user explicitly opts into Modal.
- No telemetry, no accounts, no data leaves the machine except audio segments sent to Modal when that mode is explicitly enabled.
- Single-instance enforcement — only one overlay may run at a time.

## Non-goals

- Multi-user support, hosted deployment, or any form of account system.
- Microphone capture (loopback-only, by design).
- True per-application audio isolation (only per-process *labeling*, not isolation — see Risks).
- Browser-tab-level source identification (Windows audio session APIs are process-granular, not tab-granular).

## Open questions / risks

- **Audio-enhancer apps (e.g. FxSound)** can present themselves as the default output device's loopback endpoint, causing both device selection and source labeling to resolve to the enhancer process itself rather than the real app/window playing the content. Needs explicit handling (Sprint 4, PDG-60).
- **Parakeet's language gap**: doesn't cover Chinese/Japanese, and doesn't report detected language at all (a currently-open gap in NVIDIA's own model) — handled by limiting the frontend's language dropdown to what the selected engine actually supports, not by silent backend fallback.
- **Modal's live-deploy path is code-complete but not yet verified against a real Modal account** from this environment — see Sprint 3 commit history.
- **CPU latency** on sustained/fast speech is mitigated by the fallback cascade and queue preemption, but `tiny` (or Parakeet, where applicable) is the practical floor.

## Delivery plan

See the project's Linear milestones (Pdg_stack team, project "StreamScribe_adaptive") for current phase-by-phase status:

- **Sprint 1 — Backend** (done): core VAD/transcription/translation pipeline.
- **Sprint 2 — Frontend** (done): native overlay, WASAPI capture, settings.
- **Sprint 3 — Backend** (this document, Round 2): Parakeet engine, tier-mode scoping, queue preemption, bounded-latency timestamps, opt-in Modal inference, this documentation.
- **Sprint 4 — Frontend** (Round 2, next): simple/advanced view split, engine/tier/language selectors, local/Modal controls, acceptable-latency spinner and delayed-caption display logic, the FxSound device/source-detection fix.

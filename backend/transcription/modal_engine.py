"""Opt-in Modal.com GPU inference, adapted from LocalScribe_whisper_modal's
proven modal_app.py pattern (ephemeral App, serialized=True dynamically-
built class, @modal.enter() model load, max_containers=1 to avoid a
second concurrent container). Simplified for this app's shape:

- Segments are small (a few seconds of 16kHz mono audio -- well under
  Modal's per-call payload limit), so there's no need for LocalScribe's
  chunked-upload Volume machinery -- audio goes straight through as a
  function argument.
- Reuses LocalScribe's *same* model-cache Volume name
  ("localscribe-whisper-model-cache"), so faster-whisper checkpoints
  that project already downloaded there are reused, not re-fetched.
- faster-whisper only (per plan -- Parakeet-on-Modal is out of scope).
- Provisioning is on-demand (triggered by the frontend's "Set up Modal
  instance" button) and explicitly stoppable, not kept continuously
  warm -- see main.py's modal_setup_status handling.
- Assumes ambient Modal CLI authentication (`modal token new`, already
  set up on this machine from using LocalScribe) rather than LocalScribe's
  per-request token_id/secret plumbing, which existed there for a
  multi-user product; this is a single-user local app.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

import numpy as np

from backend.config import BEAM_SIZE, WHISPER_TEMPERATURE
from backend.transcription.engine import TranscriptionResult

MODEL_CACHE_DIR = "/cache/huggingface"
DEFAULT_MODAL_TIER = "small"
DEFAULT_GPU = "T4"

# `modal` itself, and everything below that touches it (Volume/Image
# references, App construction), is imported/constructed lazily -- inside
# functions, never at module level -- so simply importing this module (as
# main.py does unconditionally, to register "modal" as a selectable
# engine) never requires Modal credentials or network access. Only
# actually invoking deploy_and_warm_up() does.


def _build_remote_cls(model_name: str):
    import modal
    # Built dynamically (per warm-up) so model_name can be closed over
    # without a modal.parameter() -- mirrors LocalScribe's _build_transcriber_cls.
    class _RemoteEngine:
        @modal.enter()
        def load_model(self):
            from faster_whisper import WhisperModel
            self.model = WhisperModel(model_name, device="cuda", compute_type="float16", download_root=MODEL_CACHE_DIR)

        @modal.method()
        def ping(self) -> bool:
            return True

        @modal.method()
        def transcribe(self, audio_bytes: bytes, sample_rate: int, language: str | None) -> dict:
            audio = np.frombuffer(audio_bytes, dtype=np.float32)
            segments, info = self.model.transcribe(
                audio, language=language, beam_size=BEAM_SIZE, temperature=WHISPER_TEMPERATURE
            )
            text = " ".join(s.text.strip() for s in segments).strip()
            return {"text": text, "detected_lang": info.language}

    return _RemoteEngine


class ModalEngine:
    """A process-wide singleton (see main.py's _get_modal_engine), NOT one
    instance per WebSocket connection -- a websocket reconnect (a network
    blip, the frontend restarting) must not lose track of an already-live
    Modal container. Since only one App/container is ever deployed at a
    time (max_containers=1), there's only ever one real "handle" to keep
    track of regardless of how many frontend connections come and go.

    Modal state (the App/container) only exists between
    deploy_and_warm_up() and stop() -- calling transcribe_segment() before
    deploy_and_warm_up() (or after stop()) is a caller error, not
    something this class silently handles, since main.py only reaches it
    while state.engine_name == "modal" and the frontend only offers Modal
    after setup reports "ready"."""

    def __init__(self, model_name: str = DEFAULT_MODAL_TIER, gpu: str = DEFAULT_GPU) -> None:
        self.model_name = model_name
        self.gpu = gpu
        self._app = None
        self._run_ctx = None
        self._instance = None
        # The "handle" main.py's modal_setup_status events surface, and
        # what gets logged -- Modal's own identifier for this App run, so
        # there's a concrete answer to "which deployment is this" instead
        # of just an in-memory Python object no one outside this process
        # can inspect. None whenever no App is currently deployed.
        self.app_id: str | None = None
        self._rtf_samples: list[float] = []
        # See AdaptiveEngine._state_lock: transcribe_segment() runs inside
        # asyncio.to_thread, and backend/main.py's PARALLEL_WORKERS can put
        # several calls in flight on different OS threads at once, all
        # mutating _rtf_samples. (The remote calls themselves don't
        # actually run concurrently on Modal -- max_containers=1 means
        # Modal queues them server-side -- so this doesn't buy Modal any
        # real parallelism, just keeps the local bookkeeping race-free.)
        self._state_lock = threading.Lock()

    @property
    def is_active(self) -> bool:
        return self._run_ctx is not None

    @property
    def model_tier(self) -> str:
        return f"modal:{self.model_name}"

    def deploy_and_warm_up(
        self, on_status: Callable[[str], None], token_id: str | None = None, token_secret: str | None = None,
    ) -> None:
        """Synchronous -- call via asyncio.to_thread from main.py. Reports
        "deploying" -> "warming up" -> "ready" through on_status, matching
        the plan's modal_setup_status event stream.

        `token_id`/`token_secret`, if given, authenticate this call via
        Modal's documented MODAL_TOKEN_ID/MODAL_TOKEN_SECRET env vars
        instead of relying on the ambient `modal token set` CLI login this
        module's docstring originally assumed -- that assumption doesn't
        hold once this runs inside Docker, since the container doesn't
        inherit the host's ~/.modal.toml. Left as None/blank, falls back
        to whatever ambient auth the container happens to have (unchanged
        behavior). Set *before* `import modal`: the SDK reads them at
        first use, and this module deliberately imports modal lazily so
        merely importing this file never requires credentials."""
        if token_id and token_secret:
            os.environ["MODAL_TOKEN_ID"] = token_id
            os.environ["MODAL_TOKEN_SECRET"] = token_secret

        import modal

        on_status("deploying")
        image = (
            modal.Image.from_registry("nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04", add_python="3.12")
            .pip_install("faster-whisper")
        )
        model_cache_volume = modal.Volume.from_name("localscribe-whisper-model-cache", create_if_missing=True)

        self._app = modal.App("streamscribe-adaptive-modal")
        remote_cls = self._app.cls(
            image=image, gpu=self.gpu, timeout=300,
            volumes={"/cache": model_cache_volume},
            serialized=True,
            max_containers=1,
            scaledown_window=1800,
        )(_build_remote_cls(self.model_name))
        self._instance = remote_cls()

        self._run_ctx = self._app.run()
        self._run_ctx.__enter__()
        # The concrete identifier for this deployment -- see app_id's own
        # comment on why this is recorded at all, rather than just holding
        # onto the (opaque, in-process-only) App object.
        self.app_id = self._app.app_id

        on_status("warming up")
        call = self._instance.ping.spawn()
        call.get(timeout=180)
        on_status("ready")

    def ping(self, timeout: float = 30.0) -> bool:
        """A real round trip to the actual deployed container, not just a
        check of this process's own believe-it's-active bookkeeping (see
        is_active) -- confirms Modal hasn't scaled it down or otherwise
        ended it out from under us. Used by main.py's heartbeat to report
        genuine "alive" status rather than an assumed one. False (never
        raises) for anything that means this instance is no longer usable:
        no deployment at all, the call failing, or timing out."""
        if not self.is_active:
            return False
        try:
            call = self._instance.ping.spawn()
            return bool(call.get(timeout=timeout))
        except Exception:
            return False

    def stop(self) -> None:
        if self._run_ctx is not None:
            self._run_ctx.__exit__(None, None, None)
            self._run_ctx = None
        self._app = None
        self._instance = None
        self.app_id = None

    def transcribe_segment(
        self, audio: np.ndarray, sample_rate: int = 16000, language: str | None = None
    ) -> TranscriptionResult:
        """Strain here is measured the same way as local RTF -- see the
        plan's ASR research section on why Modal needs no separate signal:
        end-to-end call time already reflects whatever is slowing Modal
        down (GPU load, its own queueing, a cold start)."""
        duration = len(audio) / sample_rate
        start = time.monotonic()
        result = self._instance.transcribe.remote(audio.astype(np.float32).tobytes(), sample_rate, language)
        elapsed = time.monotonic() - start

        rtf = elapsed / duration if duration > 0 else 0.0
        with self._state_lock:
            self._rtf_samples.append(rtf)
            self._rtf_samples = self._rtf_samples[-4:]
            avg_rtf = sum(self._rtf_samples) / len(self._rtf_samples)
        cpu_status = "red" if avg_rtf > 1.0 else "yellow" if avg_rtf > 0.6 else "green"

        return TranscriptionResult(
            text=result["text"],
            detected_lang=result["detected_lang"],
            model_tier=self.model_tier,
            cpu_status=cpu_status,
        )

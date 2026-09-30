"""Central tunables for the backend. Values here are planning-grade
defaults (per the implementation plan) meant to be tuned empirically
against real audio during Sprint 1 testing, not treated as final."""

import os

BEAM_SIZE = 5

# ASR fallback cascade, most-accurate first. "tiny" is the floor -- no
# further fallback below it.
MODEL_TIERS = ["small", "base", "tiny"]

SAMPLE_RATE = 16000
FRAME_MS = 30  # webrtcvad only accepts 10/20/30ms frames

VAD_AGGRESSIVENESS = 2  # 0 (least aggressive) - 3 (most aggressive)
SILENCE_TRIGGER_MS = 500
MAX_SEGMENT_S = 10
PARTIAL_INTERVAL_S = 3

# RTF (processing time / audio duration) thresholds driving tier fallback.
# avg RTF <= RTF_GREEN_MAX -> green (headroom to step back up a tier)
# avg RTF <= RTF_YELLOW_MAX -> yellow (keeping up, but tight)
# avg RTF >  RTF_YELLOW_MAX -> red (falling behind, step down a tier)
RTF_GREEN_MAX = 0.6
RTF_YELLOW_MAX = 1.0
STRAIN_WINDOW = 4  # consecutive segments averaged before switching tiers

NLLB_MODEL_NAME = "facebook/nllb-200-distilled-600M"
NLLB_MODEL_DIR = "/root/.cache/streamscribe/nllb-200-distilled-600M-ct2"
DEFAULT_DST_LANG = "en"
DEFAULT_SRC_LANG = "auto"
DEFAULT_TIER_MODE = "auto"
DEFAULT_ENGINE = "faster-whisper"

# Parakeet TDT 0.6B v3, via sherpa-onnx's quantized ONNX export -- local
# only, never via Modal (see plan). Covers Russian/Spanish/English/
# Italian/Portuguese; the frontend's language dropdown limits selectable
# languages to what's actually available per engine.
PARAKEET_MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2"
PARAKEET_CACHE_DIR = "/root/.cache/streamscribe/parakeet"

# Queue preemption: if the predicted wait for the newest queued segment
# (queue depth ahead of it * recent average processing time) exceeds this
# multiple of the user's acceptable-latency setting, drop everything queued
# except the newest segment so processing catches up to "now".
QUEUE_PREEMPTION_FACTOR = 1.2
PROCESSING_TIME_WINDOW = 4  # segments averaged for the preemption estimate
DEFAULT_ACCEPTABLE_LATENCY_S = 1

# Queue-length tier fallback: a second, more urgent strain signal alongside
# RTF_* above (faster-whisper only -- Parakeet has no tiers to fall back
# through). A burst of segments can back the queue up even when each one
# individually still looks fine to RTF, so in "auto" tier_mode try a
# lighter model tier first -- it's reversible and may well let the queue
# drain on its own -- before ever resorting to queue preemption (which
# drops segments outright and can't be undone for those segments). Steps
# back up the same way once the backlog clears. Uses a shorter window than
# STRAIN_WINDOW since backlog is a direct, current symptom rather than a
# lagging average.
QUEUE_LENGTH_DEMOTE_DEPTH = 2   # avg segments-ahead above this -> step down a tier
QUEUE_LENGTH_PROMOTE_DEPTH = 0  # avg segments-ahead at/below this -> try stepping back up
QUEUE_STRAIN_WINDOW = 2

# How many segments the backend transcribes AT THE SAME TIME. 1 = fully
# serial (today's setting): only ever one segment being worked on, next one
# waits its turn -- simplest possible behavior, easiest to reason about
# when debugging. Historically this was set automatically from CPU count
# (2, 3, or 4 workers depending on the machine) so a burst of speech could
# be processed in parallel instead of queueing up -- see git history for
# that formula if higher throughput is wanted again later. Pinned back to
# 1 for now to rule out concurrency as a source of bugs while diagnosing a
# "queue gets stuck" report. GPU (Modal) is unaffected either way -- Modal's
# own container stays single-flight by design (see modal_engine.py's
# max_containers=1), a separate concern from local CPU parallelism.
PARALLEL_WORKERS = 1
_cpu_count = os.cpu_count() or 2
CPU_THREADS_PER_WORKER = max(1, _cpu_count // PARALLEL_WORKERS)

# Hard ceiling on how long a single segment's transcribe+translate call may
# run before it's given up on. Confirmed via logs: with PARALLEL_WORKERS=1,
# a single abnormally slow model.transcribe() call (e.g. the machine's CPU
# was under heavy contention from something else entirely -- another
# process competing for the same cores will do this) can leave the ONE
# worker permanently stuck waiting on it, since nothing else exists to pick
# up new segments in the meantime -- the queue then only ever grows,
# forever, with no further transcript ever produced. This value is well
# above every processing time actually observed in testing (worst case so
# far ~9s) but still finite, so one pathologically slow call becomes "this
# one segment is skipped" instead of "the entire pipeline is now dead."
PROCESSING_TIMEOUT_S = 30.0

"""Central tunables for the backend. Values here are planning-grade
defaults (per the implementation plan) meant to be tuned empirically
against real audio during Sprint 1 testing, not treated as final."""

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

# Queue preemption: if the predicted wait for the newest queued segment
# (queue depth ahead of it * recent average processing time) exceeds this
# multiple of the user's acceptable-latency setting, drop everything queued
# except the newest segment so processing catches up to "now".
QUEUE_PREEMPTION_FACTOR = 1.2
PROCESSING_TIME_WINDOW = 4  # segments averaged for the preemption estimate
DEFAULT_ACCEPTABLE_LATENCY_S = 1

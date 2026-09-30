"""Best-effort text-based language ID, for engines that transcribe correctly
in whatever language the audio actually is but don't report which one --
currently only Parakeet (see parakeet_engine.py's docstring: a known,
open gap in NVIDIA's own model, not something fixable on our side).

Without this, main.py's translate() call received `src=None` for every
Parakeet segment, and nllb_code(None) silently falls back to "eng_Latn" --
meaning translation was either skipped entirely (destination also English:
looked like "no bug," just no translation) or ran with the WRONG assumed
source language for any other destination (garbage output, silently, no
error). Guessing from the transcribed text -- restricted to just Parakeet's
5 known-supported languages, which makes a statistical guesser far more
reliable than picking from every language it knows -- turns both of those
into "translate correctly, most of the time" instead.

Uses `langid` (pure Python + a bundled pretrained model, no network/model
download at runtime, unlike NLLB/Whisper) rather than anything heavier --
this only ever needs to pick among a handful of candidates for one short
transcribed sentence at a time.
"""

from __future__ import annotations

import threading

import langid

# langid's set_languages()/classify() mutate its one shared, module-global
# classifier -- fine for today's single processor worker (config.py's
# PARALLEL_WORKERS), but set_languages-then-classify must stay atomic
# together, or two concurrent guesses could interleave and one call could
# classify against the other's candidate set. Cheap enough per call (one
# short sentence) that serializing here costs nothing noticeable.
_lock = threading.Lock()


def guess_language(text: str, candidates: list[str] | None = None) -> str | None:
    """Returns a best-guess ISO 639-1 code for `text`, or None if there's
    nothing to guess from. `candidates`, when given, restricts the guess to
    just those languages -- pass the active engine's known-supported set
    when possible; a guess among a handful of real candidates is far more
    reliable than an unconstrained guess among everything langid knows."""
    if not text.strip():
        return None
    with _lock:
        langid.set_languages(candidates or None)
        lang, _confidence = langid.classify(text)
        return lang

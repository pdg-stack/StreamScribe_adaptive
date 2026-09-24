"""Translation via NLLB-200-distilled-600M (CTranslate2, int8 on CPU),
behind a swappable translate(text, src, dst) interface. Replaces the
earlier Argos Translate proposal once "support all languages" became a
requirement -- Argos only covers ~35-40 pairs; NLLB-200 covers ~200
languages in one model, and reuses CTranslate2, the runtime faster-whisper
already depends on.

The CT2 model + tokenizer are converted/downloaded once, on first use, into
NLLB_MODEL_DIR (under the same Docker volume faster-whisper caches into),
so runtime after that is fully offline. Conversion needs transformers+torch
once; neither is required for the ASR path.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

# The Xet fast-download backend (huggingface_hub's default for repos that
# support it) was observed to stall indefinitely partway through this
# model's ~2.5GB checkpoint in testing. Falling back to plain HTTP download
# is slower but reliable -- must be set before huggingface_hub's first
# network call, so this runs at import time, ahead of the ctranslate2/spm
# imports below (which pull huggingface_hub in transitively).
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import ctranslate2
import sentencepiece as spm

from backend.config import NLLB_MODEL_DIR, NLLB_MODEL_NAME

# ISO 639-1 (Whisper's detected-language codes) -> NLLB-200 FLORES-200 code.
# Not exhaustive -- covers the languages most likely to actually show up.
# Unmapped codes fall back to "eng_Latn": translation still runs, just
# assumes an English-like framing for an unrecognized source, which is the
# least surprising failure mode for arbitrary audio. Extend as needed.
ISO_TO_NLLB = {
    "en": "eng_Latn", "hi": "hin_Deva", "zh": "zho_Hans", "es": "spa_Latn",
    "fr": "fra_Latn", "de": "deu_Latn", "ja": "jpn_Jpan", "ko": "kor_Hang",
    "ar": "arb_Arab", "ru": "rus_Cyrl", "pt": "por_Latn", "it": "ita_Latn",
    "bn": "ben_Beng", "ur": "urd_Arab", "ta": "tam_Taml", "te": "tel_Telu",
    "mr": "mar_Deva", "gu": "guj_Gujr", "kn": "kan_Knda", "ml": "mal_Mlym",
    "pa": "pan_Guru", "vi": "vie_Latn", "th": "tha_Thai", "id": "ind_Latn",
    "tr": "tur_Latn", "pl": "pol_Latn", "nl": "nld_Latn", "sv": "swe_Latn",
    "fa": "pes_Arab", "he": "heb_Hebr", "uk": "ukr_Cyrl", "cs": "ces_Latn",
    "ro": "ron_Latn", "el": "ell_Grek", "hu": "hun_Latn", "fi": "fin_Latn",
    "da": "dan_Latn", "no": "nob_Latn", "sk": "slk_Latn", "bg": "bul_Cyrl",
}

_lock = threading.Lock()
_translator: ctranslate2.Translator | None = None
_tokenizer: spm.SentencePieceProcessor | None = None


def nllb_code(iso_639_1: str) -> str:
    return ISO_TO_NLLB.get(iso_639_1, "eng_Latn")


def _ensure_ct2_model(model_dir: Path) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)

    if not (model_dir / "model.bin").exists():
        from ctranslate2.converters import TransformersConverter

        TransformersConverter(NLLB_MODEL_NAME).convert(
            str(model_dir), quantization="int8", force=True
        )

    spm_path = model_dir / "sentencepiece.bpe.model"
    if not spm_path.exists():
        from huggingface_hub import hf_hub_download

        downloaded = hf_hub_download(NLLB_MODEL_NAME, "sentencepiece.bpe.model")
        shutil.copy(downloaded, spm_path)


def _load() -> tuple[ctranslate2.Translator, spm.SentencePieceProcessor]:
    global _translator, _tokenizer
    with _lock:
        if _translator is None:
            model_dir = Path(NLLB_MODEL_DIR)
            _ensure_ct2_model(model_dir)
            _translator = ctranslate2.Translator(str(model_dir), device="cpu", compute_type="int8")
            _tokenizer = spm.SentencePieceProcessor(str(model_dir / "sentencepiece.bpe.model"))
        return _translator, _tokenizer


def translate(text: str, src: str, dst: str = "en") -> str:
    """src/dst are ISO 639-1 codes (matching Whisper's detected_lang and
    the frontend's destination picker)."""
    if not text.strip() or src == dst:
        return text

    translator, tokenizer = _load()
    src_lang, dst_lang = nllb_code(src), nllb_code(dst)

    source_tokens = [src_lang] + tokenizer.encode(text, out_type=str) + ["</s>"]
    results = translator.translate_batch([source_tokens], target_prefix=[[dst_lang]])
    output_tokens = results[0].hypotheses[0][1:]  # drop the target_prefix token
    return tokenizer.decode(output_tokens)

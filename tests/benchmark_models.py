"""Benchmark faster-whisper models against a reference transcript.

Not part of the package — a one-off harness kept for re-running when the
recording set grows. Usage:

    python tests/benchmark_models.py tiny base small
    python tests/benchmark_models.py --list
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

TESTS = Path(__file__).resolve().parent
AUDIO = TESTS / "recordings" / "transcript_1_v1.wav"
REFERENCE = TESTS / "transcripts" / "transcript_1.txt"
RESULTS = TESTS / "benchmark_results.json"

# The vocabulary hint the app ships with, normalised the way stt.build_prompt
# does it (trailing period matters — see stt.build_prompt docstring).
VOCAB = (
    "padel, bandeja, vibora, chiquita, remate, revés, dejada, globo, "
    "lumbar, glute bridge, hamstring, dorsiflexion, scapula."
)

# Content words whose loss would actually cost a retrieval later.
# Each entry is a list of ACCEPTABLE spellings: a term counts as recalled if
# any variant appears. This matters because the goal is retrieval, not
# stenography — the reference says "cyber seguridad" but a model writing
# "ciberseguridad" has preserved the meaning and stays searchable.
# NOTE: the first variant must appear literally in the reference transcript
# (`test_key_terms_appear_in_reference` enforces it) — an earlier version
# scored only "ciberseguridad" and silently capped every model's recall.
KEY_TERMS = [
    ["padel", "pádel"],
    ["lumbares"],
    ["core"],
    ["gym"],
    ["milanesas", "milanesa"],
    ["papas fritas"],
    ["bandeja"],
    ["ternera", "terneras"],
    ["age of empires", "ace of empire", "age of empire"],
    ["cyber seguridad", "ciberseguridad", "cíber seguridad"],
    ["hacking"],
    ["mba"],
    ["conferencias"],
]

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalise(text: str, *, strip_accents: bool = False) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Optionally deaccent."""
    text = unicodedata.normalize("NFC", text).lower()
    text = _PUNCT.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()
    if strip_accents:
        text = "".join(
            c for c in unicodedata.normalize("NFD", text)
            if unicodedata.category(c) != "Mn"
        )
    return text


def levenshtein(a: list[str], b: list[str]) -> int:
    """Edit distance between token lists (iterative, two rows)."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, token_a in enumerate(a, 1):
        current = [i]
        for j, token_b in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + (token_a != token_b),  # substitution
            ))
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str, **kw) -> float:
    ref = normalise(reference, **kw).split()
    hyp = normalise(hypothesis, **kw).split()
    if not ref:
        return 0.0
    return levenshtein(ref, hyp) / len(ref)


def term_recall(reference: str, hypothesis: str) -> tuple[float, list[str]]:
    """Fraction of key content terms that survived, in any accepted spelling.

    Accent-insensitive: a memo is searchable whether or not Whisper wrote
    "padel" or "pádel".
    """
    hyp = normalise(hypothesis, strip_accents=True)
    missing = [
        variants[0]
        for variants in KEY_TERMS
        if not any(normalise(v, strip_accents=True) in hyp for v in variants)
    ]
    recalled = len(KEY_TERMS) - len(missing)
    return recalled / len(KEY_TERMS), missing


def run_model(name: str, *, language: str | None, vocabulary: str | None) -> dict:
    from faster_whisper import WhisperModel

    load_start = time.perf_counter()
    model = WhisperModel(name, device="cpu", compute_type="int8")
    load_s = time.perf_counter() - load_start

    transcribe_start = time.perf_counter()
    segments, info = model.transcribe(
        str(AUDIO),
        language=language,
        vad_filter=True,
        initial_prompt=vocabulary,
    )
    text = "".join(s.text for s in segments).strip()
    transcribe_s = time.perf_counter() - transcribe_start

    reference = REFERENCE.read_text(encoding="utf-8").strip()
    recall, missing = term_recall(reference, text)
    return {
        "model": name,
        "language": language or "auto",
        "vocabulary": bool(vocabulary),
        "detected_language": getattr(info, "language", None),
        "load_s": round(load_s, 1),
        "transcribe_s": round(transcribe_s, 1),
        "realtime_factor": round(transcribe_s / 80.9, 2),
        "wer": round(word_error_rate(reference, text), 4),
        "wer_no_accents": round(
            word_error_rate(reference, text, strip_accents=True), 4
        ),
        "term_recall": round(recall, 3),
        "missing_terms": missing,
        "chars": len(text),
        "text": text,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="*", default=["tiny", "base"])
    parser.add_argument("--language", default="es")
    parser.add_argument("--no-vocab", action="store_true")
    parser.add_argument("--auto-language", action="store_true")
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    results = json.loads(RESULTS.read_text()) if RESULTS.exists() else []

    for name in args.models:
        label = f"{name} lang={'auto' if args.auto_language else args.language} " \
                f"vocab={not args.no_vocab}{' ' + args.tag if args.tag else ''}"
        print(f"--- {label}", flush=True)
        try:
            row = run_model(
                name,
                language=None if args.auto_language else args.language,
                vocabulary=None if args.no_vocab else VOCAB,
            )
        except Exception as exc:
            print(f"    FAILED: {type(exc).__name__}: {exc}", flush=True)
            continue
        row["tag"] = args.tag
        results = [r for r in results
                   if not (r["model"] == row["model"]
                           and r["language"] == row["language"]
                           and r["vocabulary"] == row["vocabulary"]
                           and r.get("tag", "") == row["tag"])]
        results.append(row)
        RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"    WER {row['wer']:.3f} | recall {row['term_recall']:.2f} | "
              f"{row['transcribe_s']}s ({row['realtime_factor']}x realtime)",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for the model-benchmark harness and the config it produced.

These don't run Whisper — they guard the scoring logic (a bug there silently
misranks models) and the deployment invariants the benchmark revealed.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pytest
import yaml

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT / "src"))

import benchmark_models as bm  # noqa: E402

# Peak RSS measured on this machine, in MB (see tests/BENCHMARK.md).
PEAK_RSS_MB = {"tiny": 250, "base": 445, "small": 893, "medium": 2570, "large-v3": 4559}


def _deaccent(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


# --- scoring logic --------------------------------------------------------


def test_key_terms_appear_in_reference():
    """Each term's first spelling must really be in the reference.

    An earlier version scored "ciberseguridad" while the reference said
    "cyber seguridad", so every model was marked wrong on a term none could
    get right — which quietly capped the whole comparison.
    """
    reference = _deaccent(bm.normalise(bm.REFERENCE.read_text(encoding="utf-8")))
    missing = [
        variants[0]
        for variants in bm.KEY_TERMS
        if _deaccent(bm.normalise(variants[0])) not in reference
    ]
    assert missing == [], f"key terms absent from the reference transcript: {missing}"


def test_perfect_transcript_scores_perfectly():
    reference = bm.REFERENCE.read_text(encoding="utf-8").strip()
    assert bm.word_error_rate(reference, reference) == 0.0
    recall, missing = bm.term_recall(reference, reference)
    assert recall == 1.0 and missing == []


def test_empty_transcript_scores_worst():
    reference = bm.REFERENCE.read_text(encoding="utf-8").strip()
    assert bm.word_error_rate(reference, "") == 1.0
    recall, _ = bm.term_recall(reference, "")
    assert recall == 0.0


def test_wer_is_punctuation_and_case_insensitive():
    assert bm.word_error_rate("Hola, mundo.", "hola mundo") == 0.0


def test_wer_counts_a_substitution():
    assert bm.word_error_rate("uno dos tres", "uno dos cuatro") == pytest.approx(1 / 3)


def test_term_recall_accepts_spelling_variants():
    """Retrieval, not stenography: an accepted variant still finds the memo."""
    recall_a, _ = bm.term_recall("", "hablamos de padel y de cyber seguridad")
    recall_b, _ = bm.term_recall("", "hablamos de pádel y de ciberseguridad")
    assert recall_a == recall_b > 0


def test_term_recall_is_accent_insensitive():
    plain, _ = bm.term_recall("", "jugaba al padel")
    accented, _ = bm.term_recall("", "jugaba al pádel")
    assert plain == accented > 0


def test_levenshtein_basics():
    assert bm.levenshtein(["a", "b"], ["a", "b"]) == 0
    assert bm.levenshtein(["a", "b"], ["a"]) == 1
    assert bm.levenshtein([], ["a", "b"]) == 2


# --- the benchmark's conclusions, pinned ----------------------------------


def test_benchmark_fixtures_exist():
    assert bm.AUDIO.is_file(), "benchmark recording is missing"
    assert bm.REFERENCE.is_file(), "reference transcript is missing"


def test_vocabulary_constant_is_covered_by_shipped_default():
    """The benchmark must never score a prompt richer than the app's own.

    `bm.VOCAB` is frozen at the wording used to produce
    `benchmark_results.json`; the shipped list keeps growing as jargon misses
    turn up (BENCHMARK.md tells you to add them). Growth is fine — the numbers
    stay honest as long as every term the benchmark scored is still shipped.
    Re-run the benchmark and refresh `bm.VOCAB` if you want credit for a term
    added later.
    """
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("MINDBACKUP_VOCABULARY="):
            shipped = {
                term.strip().lower()
                for term in line.split("=", 1)[1].split(",")
                if term.strip()
            }
            scored = {
                term.strip().rstrip(".").lower()
                for term in bm.VOCAB.split(",")
                if term.strip()
            }
            assert scored <= shipped, f"benchmark scores unshipped terms: {scored - shipped}"
            return
    pytest.fail("MINDBACKUP_VOCABULARY missing from .env.example")


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["mindbackup"]


def _env_example_model() -> str:
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("MINDBACKUP_STT_MODEL="):
            return line.split("=", 1)[1].strip()
    pytest.fail("MINDBACKUP_STT_MODEL missing from .env.example")


def test_default_model_agrees_across_config():
    """Dockerfile ARG, compose default and .env.example must not drift.

    The runtime container is offline, so a mismatch means the configured model
    was never baked in and no memo can ever be transcribed.
    """
    from mindbackup.config import DEFAULT_MODELS

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG WHISPER_MODEL=medium" in dockerfile

    compose_arg = _compose()["build"]["args"]["WHISPER_MODEL"]
    assert compose_arg.endswith(":-medium}")

    assert _env_example_model() == "medium"
    assert DEFAULT_MODELS["local"] == "medium"


def test_memory_limit_exceeds_the_default_model_peak():
    """A container over its limit is SIGKILLed, not throttled — and with
    restart: unless-stopped that is a silent crash loop."""
    limit = _compose()["deploy"]["resources"]["limits"]["memory"]
    default = limit.split(":-")[1].rstrip("}") if ":-" in limit else limit

    assert default.endswith("g"), f"expected a GB limit, got {default!r}"
    limit_mb = float(default[:-1]) * 1024

    peak = PEAK_RSS_MB[_env_example_model()]
    assert limit_mb > peak, (
        f"memory limit {default} is below the {_env_example_model()} model's "
        f"measured peak of {peak} MB — the container would be OOM-killed"
    )

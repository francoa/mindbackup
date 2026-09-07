"""Manual end-to-end smoke of `mindbackup extract` + `ask` against a scratch
vault, with the LLM stubbed. Not part of the test suite — this exercises the
real CLI entry point (argparse, output formatting, exit codes) the way the
user will actually invoke it.

    python tests/smoke_cli.py
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

VAULT = Path(tempfile.mkdtemp(prefix="mb-smoke-"))
os.environ["OBSIDIAN_VAULT_PATH"] = str(VAULT)
os.environ["MINDBACKUP_LLM_MODEL"] = "fake/model"
os.environ["MINDBACKUP_LLM_API_KEY"] = "fake-key"

from mindbackup import extract as extract_mod  # noqa: E402
from mindbackup.__main__ import main  # noqa: E402
from mindbackup.vault import write_memo  # noqa: E402

REPLIES = {
    "2026-09-03": {
        "summary": "Search is bad; needs grip.",
        "atoms": [
            {"text": "voice-mind-backup necesita mejor búsqueda que grep.",
             "kind": "todo", "topics": ["voice-mind-backup", "search"], "confidence": 0.9},
            {"text": "Comprar grip nuevo para la pala.",
             "kind": "todo", "topics": ["padel"], "confidence": 0.95},
        ],
    },
    "2026-09-05": {
        "summary": "Back pain and a decision about topics.",
        "atoms": [
            {"text": "Decidido: usar topic pages en vez de una taxonomía fija.",
             "kind": "decision", "topics": ["voice-mind-backup"], "confidence": 0.9},
            {"text": "El estiramiento de isquiotibiales alivia la lumbar.",
             "kind": "fact", "topics": ["lower back"], "confidence": 0.9},
            {"text": "Preguntarle a él sobre aquello.",
             "kind": "todo", "topics": [], "confidence": 0.2,
             "ambiguity": "Who is 'él' and what is 'aquello'?"},
        ],
    },
}


_current = {"memo": ""}


def fake_complete_json(settings, system, user, **kwargs):
    """Route the canned reply by which memo is currently being extracted."""
    return json.loads(json.dumps(REPLIES.get(_current["memo"], {"summary": "", "atoms": []})))


# Route the stub by which memo is being extracted. The CLI imports
# extract_memo from the pipeline module at call time, so patching the module
# attribute is the seam that actually takes effect.
import mindbackup.pipeline as pipeline_mod  # noqa: E402

_real_extract_memo = pipeline_mod.extract_memo


def traced_extract_memo(memo_path, settings, **kwargs):
    _current["memo"] = memo_path.stem
    return _real_extract_memo(memo_path, settings, **kwargs)


pipeline_mod.extract_memo = traced_extract_memo
extract_mod.complete_json = fake_complete_json


def run(argv):
    print(f"\n{'=' * 70}\n$ mindbackup {' '.join(argv)}\n{'=' * 70}")
    code = main(argv)
    print(f"[exit {code}]")
    return code


try:
    from mindbackup.config import load_settings

    settings = load_settings()

    write_memo(
        "Vale, para este proyecto necesito arreglar la búsqueda, que es un grep "
        "y no encuentra nada. Y comprar grip para la pala.",
        date(2026, 9, 3), settings.memo_path,
    )
    write_memo(
        "Estuve pensando y decidí usar topic pages en vez de taxonomía. "
        "También el estiramiento de isquios me alivia la lumbar. "
        "Ah, y preguntarle a él sobre aquello.",
        date(2026, 9, 5), settings.memo_path,
    )

    failures = []

    if run(["extract"]) != 0:
        failures.append("extract")
    if run(["extract"]) != 0:
        failures.append("extract-rerun (should be a no-op)")
    if run(["ask", "--topics"]) != 0:
        failures.append("ask --topics")

    # The money query: the speaker NEVER said "voice-mind-backup" out loud.
    if run(["ask", "voice-mind-backup"]) != 0:
        failures.append("ask by canonical name")
    if run(["ask", "--kind", "todo"]) != 0:
        failures.append("ask --kind todo")
    if run(["ask", "--topic", "lower back"]) != 0:
        failures.append("ask --topic")
    if run(["ask", "nonexistent-nonsense"]) != 1:
        failures.append("ask miss should exit 1")

    print(f"\n{'=' * 70}\nTOPIC PAGES WRITTEN\n{'=' * 70}")
    for page in sorted((VAULT / "Topics").glob("*.md")):
        print(f"\n--- {page.name} ---")
        print(page.read_text(encoding="utf-8").rstrip())

    print(f"\n{'=' * 70}")
    if failures:
        print(f"FAILURES: {failures}")
        sys.exit(1)
    print("ALL SMOKE CHECKS PASSED")
finally:
    shutil.rmtree(VAULT, ignore_errors=True)

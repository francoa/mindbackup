"""`mindbackup delete`: a memo goes, and so does everything filed from it."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from mindbackup import __main__ as cli
from mindbackup import topics
from mindbackup.config import Settings
from mindbackup.extract import Atom
from mindbackup.vault import write_memo


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    settings = Settings(vault_path=tmp_path / "vault")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    return settings


def _memo_with_atoms(settings: Settings, texts: list[tuple[str, list[str]]]) -> Path:
    memo = write_memo("some transcript", date(2026, 9, 18), settings.memo_path)
    atoms = [Atom(text=text, kind="fact", topics=t) for text, t in texts]
    topics.file_atoms(atoms, memo.path.stem, memo.memo_date, settings)
    return memo.path


@pytest.fixture
def two_memos(settings):
    doomed = _memo_with_atoms(settings, [("Padel grip is worn.", ["padel"]), ("No topic.", [])])
    kept = _memo_with_atoms(settings, [("Padel on Sunday.", ["padel"])])
    return doomed, kept


def _index_memos(settings) -> list[str]:
    return [stored.memo for stored in topics.iter_index(settings)]


def test_delete_removes_memo_atoms_and_bullets(settings, two_memos):
    doomed, kept = two_memos
    page = topics.topic_page_path("padel", settings)
    page.write_text(page.read_text(encoding="utf-8") + f"my note on [[{doomed.stem}]]\n")

    assert cli.main(["delete", doomed.stem, "--yes"]) == 0

    assert not doomed.exists()
    assert kept.exists()
    assert _index_memos(settings) == [kept.stem]
    text = page.read_text(encoding="utf-8")
    assert "Padel grip is worn." not in text
    assert "Padel on Sunday." in text
    assert f"my note on [[{doomed.stem}]]" in text  # hand-written line survives
    default_page = topics.topic_page_path(topics.DEFAULT_TOPIC, settings)
    assert "No topic." not in default_page.read_text(encoding="utf-8")


def test_dry_run_deletes_nothing(settings, two_memos):
    doomed, _ = two_memos
    before = topics.index_path(settings).read_text(encoding="utf-8")

    assert cli.main(["delete", f"{doomed.name}", "--dry-run"]) == 0

    assert doomed.exists()
    assert topics.index_path(settings).read_text(encoding="utf-8") == before


def test_declining_the_prompt_deletes_nothing(settings, two_memos, monkeypatch):
    doomed, _ = two_memos
    monkeypatch.setattr("builtins.input", lambda _: "n")

    assert cli.main(["delete", doomed.stem]) == 1
    assert doomed.exists()
    assert doomed.stem in _index_memos(settings)


def test_orphans_are_cleaned_when_memo_file_is_already_gone(settings, two_memos):
    doomed, kept = two_memos
    doomed.unlink()

    assert cli.main(["delete", doomed.stem, "--yes"]) == 0
    assert _index_memos(settings) == [kept.stem]


def test_unknown_memo_is_an_error(settings, two_memos):
    assert cli.main(["delete", "1999-01-01", "--yes"]) == 1


def test_bullet_is_removed_even_if_index_line_is_missing(settings, two_memos):
    doomed, kept = two_memos
    path = topics.index_path(settings)
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["memo"] != doomed.stem
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    removal = topics.remove_memo(doomed.stem, settings)

    assert removal.atoms == []
    assert sum(removal.bullets.values()) == 2

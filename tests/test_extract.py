"""End-to-end check of the extraction + retrieval layer against a fake LLM.

Deliberately does NOT hit the network: it stubs mindbackup.llm.complete_json so
the real extract/file/search code paths run over a realistic model reply. This
is the test that proves `ask` finds an atom the speaker never named explicitly.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from mindbackup import extract as extract_mod
from mindbackup import pipeline, topics
from mindbackup.config import Settings
from mindbackup.extract import Atom, extract_atoms
from mindbackup.vault import write_memo

# A rambling memo where the speaker says "this project", never the name.
TRANSCRIPT = (
    "Vale, entonces, eh, estaba pensando en esto mientras volvía del padel. "
    "Para este proyecto necesito arreglar el tema de la búsqueda, que ahora "
    "mismo es un grep y no encuentra nada. Y bueno, también, eh, tengo que "
    "acordarme de comprar grip nuevo para la pala. Ya está, nada más."
)

FAKE_REPLY = {
    "summary": "Search is inadequate; also needs new padel grip.",
    "atoms": [
        {
            "text": "voice-mind-backup necesita una búsqueda mejor que grep.",
            "kind": "todo",
            "topics": ["voice-mind-backup", "search"],
            "confidence": 0.9,
            "ambiguity": None,
        },
        {
            "text": "Comprar grip nuevo para la pala de padel.",
            "kind": "todo",
            "topics": ["padel"],
            "confidence": 0.95,
            "ambiguity": None,
        },
        {
            "text": "Hablar con el de siempre sobre lo del otro día.",
            "kind": "todo",
            "topics": [],
            "confidence": 0.3,
            "ambiguity": "Who is 'el de siempre' and what was discussed?",
        },
    ],
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        vault_path=tmp_path / "vault",
        llm_model="fake/model",
        llm_api_key="fake-key",
    )


@pytest.fixture
def fake_llm(monkeypatch):
    """Stub the model, capturing the prompt it was given."""
    captured = {}

    def _fake(settings, system, user, **kwargs):
        captured["system"] = system
        captured["user"] = user
        return json.loads(json.dumps(FAKE_REPLY))

    monkeypatch.setattr(extract_mod, "complete_json", _fake)
    return captured


def test_extract_parses_atoms(settings, fake_llm):
    result = extract_atoms(TRANSCRIPT, settings)

    assert len(result.atoms) == 3
    assert result.atoms[0].kind == "todo"
    assert "voice-mind-backup" in result.atoms[0].topics
    # The low-confidence atom is flagged, not silently filed.
    assert len(result.ambiguous) == 1
    assert result.ambiguous[0].ambiguity


def test_known_topics_are_offered_to_the_model(settings, fake_llm):
    extract_atoms(TRANSCRIPT, settings, ["padel", "lower back"])
    assert "padel" in fake_llm["user"]
    assert "lower back" in fake_llm["user"]


def test_bad_kind_and_topics_are_normalised(settings, monkeypatch):
    monkeypatch.setattr(
        extract_mod,
        "complete_json",
        lambda *a, **k: {
            "atoms": [
                {"text": "x", "kind": "nonsense", "topics": "  #Padel  ", "confidence": "high"},
                {"text": "", "kind": "todo", "topics": []},
            ]
        },
    )
    result = extract_atoms("something", settings)

    assert len(result.atoms) == 1, "empty-text atoms must be dropped"
    assert result.atoms[0].kind == "fact", "unknown kind falls back"
    assert result.atoms[0].topics == ["padel"], "topics normalised, # stripped"
    assert result.atoms[0].confidence == 1.0, "unparseable confidence falls back"


def test_extract_memo_files_every_atom_flagging_the_ambiguous_one(settings, fake_llm):
    memo = write_memo(TRANSCRIPT, date(2026, 9, 6), settings.memo_path)
    result = pipeline.extract_memo(memo.path, settings)

    assert len(result.atoms) == 3
    # An ambiguous atom is filed rather than dropped, carrying the question
    # with it, so nothing said is lost while clarification is still manual.
    assert len(result.filed) == 3
    ambiguous = [a for a in result.filed if "AMBIGUITY:" in a.text]
    assert len(ambiguous) == 1, "the ambiguous atom is filed, marked as such"
    assert "el de siempre" in ambiguous[0].text

    # Topic pages exist and carry a link home to the memo.
    page = settings.topic_path / "voice-mind-backup.md"
    assert page.is_file()
    content = page.read_text(encoding="utf-8")
    assert "búsqueda mejor que grep" in content
    assert f"[[{memo.path.stem}]]" in content, "atom must reference its source memo"
    assert "^mb-" in content, "block ref needed for idempotent re-runs"


def test_atoms_with_no_topic_land_on_the_default_page(settings, fake_llm):
    """An atom the model could not classify still has to surface in Obsidian."""
    memo = write_memo(TRANSCRIPT, date(2026, 9, 6), settings.memo_path)
    pipeline.extract_memo(memo.path, settings)

    page = settings.topic_path / f"{topics.topic_slug(topics.DEFAULT_TOPIC)}.md"
    assert page.is_file(), "the untopiced atom needs somewhere to live"
    content = page.read_text(encoding="utf-8")
    assert "el de siempre" in content
    assert "grip nuevo" not in content, "atoms with a topic of their own stay off it"

    # The default page is a filing convenience, not a claim about the atom:
    # the index must still show it as untopiced so a later pass can classify it.
    untopiced = [a for a in topics.iter_index(settings) if "el de siempre" in a.text]
    assert len(untopiced) == 1
    assert untopiced[0].topics == []

    pipeline.extract_memo(memo.path, settings)
    bullets = [l for l in page.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
    assert len(bullets) == 1, "re-extraction must not duplicate the bullet"


def test_refiling_is_idempotent(settings, fake_llm):
    memo = write_memo(TRANSCRIPT, date(2026, 9, 6), settings.memo_path)
    pipeline.extract_memo(memo.path, settings)
    pipeline.extract_memo(memo.path, settings)

    page = settings.topic_path / "padel.md"
    bullets = [l for l in page.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
    assert len(bullets) == 1, "re-extraction must not duplicate bullets"

    assert sum(1 for _ in topics.iter_index(settings)) == 3, "index must not duplicate"


def test_hand_edits_to_topic_pages_survive(settings, fake_llm):
    memo = write_memo(TRANSCRIPT, date(2026, 9, 6), settings.memo_path)
    pipeline.extract_memo(memo.path, settings)

    page = settings.topic_path / "padel.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nMy own note.\n", encoding="utf-8")

    pipeline.extract_memo(memo.path, settings)
    assert "My own note." in page.read_text(encoding="utf-8")


def test_ask_finds_atom_by_name_never_spoken(settings, fake_llm):
    """The whole point: the speaker said 'este proyecto', not the name."""
    memo = write_memo(TRANSCRIPT, date(2026, 9, 6), settings.memo_path)
    pipeline.extract_memo(memo.path, settings)

    assert "voice-mind-backup" not in TRANSCRIPT, "premise: name is absent from raw speech"

    hits = topics.search(settings, "voice-mind-backup")
    assert len(hits) == 1
    assert hits[0].memo == memo.path.stem, "result points back at the source memo"


def test_ask_filters_by_kind_and_topic(settings, fake_llm):
    memo = write_memo(TRANSCRIPT, date(2026, 9, 6), settings.memo_path)
    pipeline.extract_memo(memo.path, settings)

    assert len(topics.search(settings, "", kind="todo")) == 3
    assert len(topics.search(settings, "", kind="idea")) == 0
    assert len(topics.search(settings, "", topic="padel")) == 1
    assert len(topics.search(settings, "", topic="PADEL")) == 1, "topic match is case-insensitive"


def test_read_memo_body_strips_frontmatter(settings):
    memo = write_memo("Hello there.", date(2026, 9, 6), settings.memo_path)
    body = pipeline.read_memo_body(memo.path)

    assert body == "Hello there."
    assert "type: memo" not in body


def test_extraction_failure_leaves_memo_intact(settings, monkeypatch):
    """C4: a dead LLM degrades a memo to 'unextracted', never loses it."""
    memo = write_memo(TRANSCRIPT, date(2026, 9, 6), settings.memo_path)

    def _boom(*args, **kwargs):
        raise extract_mod.LLMError("model is down")

    monkeypatch.setattr(extract_mod, "complete_json", _boom)

    with pytest.raises(extract_mod.LLMError):
        pipeline.extract_memo(memo.path, settings)

    assert memo.path.is_file()
    assert TRANSCRIPT in memo.path.read_text(encoding="utf-8")


def test_topic_slug_handles_awkward_names():
    assert topics.topic_slug("Lower Back") == "lower-back"
    assert topics.topic_slug("padel/backhand") == "padelbackhand"
    assert topics.topic_slug("  #Voice Mind Backup  ") == "voice-mind-backup"
    assert topics.topic_slug("!!!") == "untitled", "must never produce an empty filename"


def test_atoms_without_topics_are_still_searchable(settings):
    """No topic must not mean invisible — that would be a silent loss."""
    topics.file_atoms(
        [Atom(text="An orphan thought.", kind="idea", topics=[])],
        "2026-09-06",
        date(2026, 9, 6),
        settings,
    )
    assert len(topics.search(settings, "orphan")) == 1


def test_known_topics_does_not_coin_near_duplicates(settings, fake_llm):
    """A hyphenated page must not re-enter the list as a spaced variant.

    Regression: `voice-mind-backup.md` was de-slugified to "voice mind backup"
    and offered to the model alongside the real name, which is exactly the
    near-duplicate drift the known-topics list exists to prevent.
    """
    memo = write_memo(TRANSCRIPT, date(2026, 9, 6), settings.memo_path)
    pipeline.extract_memo(memo.path, settings)

    known = topics.known_topics(settings)

    assert "voice-mind-backup" in known
    assert "voice mind backup" not in known
    assert len(known) == len(set(known)), "no duplicates"


def test_known_topics_recovers_names_from_orphan_pages(settings):
    """A hand-made topic page with no filed atoms still counts as known."""
    settings.topic_path.mkdir(parents=True, exist_ok=True)
    (settings.topic_path / "lower-back.md").write_text(
        "---\ntype: topic\ntopic: lower back\n---\n\n# lower back\n", encoding="utf-8"
    )
    assert "lower back" in topics.known_topics(settings)

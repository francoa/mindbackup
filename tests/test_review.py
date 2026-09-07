"""Tests for the Telegram review layer.

The load-bearing test here is `test_extraction_failure_still_confirms_saved`:
the M1 guarantee is that a memo reaches the vault and the user is told. The
intelligence layer is allowed to fail; it is not allowed to break that.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from mindbackup import bot as bot_mod
from mindbackup import extract as extract_mod
from mindbackup.config import Settings
from mindbackup.extract import Atom, Extraction
from mindbackup.review import (
    CB_APPROVE,
    CB_DISCARD,
    PendingReview,
    apply_review,
    render_filed,
    render_review,
)
from mindbackup.topics import search
from mindbackup.vault import write_memo


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        vault_path=tmp_path / "vault",
        llm_model="fake/model",
        llm_api_key="fake-key",
        allowed_users=frozenset({42}),
    )


def _atoms() -> list[Atom]:
    return [
        Atom(text="Fix search.", kind="todo", topics=["voice-mind-backup"], confidence=0.9),
        Atom(text="Buy grip.", kind="todo", topics=["padel"], confidence=0.9),
        Atom(text="Talk to him.", kind="todo", topics=[], confidence=0.2,
             ambiguity="Who is 'him'?"),
    ]


# --- rendering -------------------------------------------------------------


def test_render_review_flags_ambiguous_atoms():
    text = render_review(Extraction(atoms=_atoms(), summary="s"), "2026-09-06.md")

    assert "Fix search." in text
    assert "⚠️" in text, "the unclear atom must be visibly flagged"
    assert "Who is 'him'?" in text, "the clarifying question must be shown"


def test_render_review_handles_empty_extraction():
    text = render_review(Extraction(atoms=[], summary=""), "2026-09-06.md")
    assert "Nothing worth extracting" in text


def test_render_review_truncates_long_lists():
    many = [Atom(text=f"Item {i}.", topics=["t"]) for i in range(30)]
    text = render_review(Extraction(atoms=many), "m.md")
    assert "and 18 more" in text, "must not blow past Telegram's message limit"


def test_render_filed_handles_nothing_filed():
    assert render_filed([]) == "Nothing filed."


# --- applying the review ---------------------------------------------------


def test_apply_review_files_only_confident_atoms(settings):
    review = PendingReview(memo_name="2026-09-06", memo_date="2026-09-06", atoms=_atoms())
    filed = apply_review(review, settings)

    assert len(filed) == 2, "the ambiguous atom must not be filed unresolved"
    assert {f.text for f in filed} == {"Fix search.", "Buy grip."}


def test_apply_review_files_resolved_ambiguous_atoms(settings):
    review = PendingReview(memo_name="2026-09-06", memo_date="2026-09-06", atoms=_atoms())
    review.resolved[2] = "coach"  # user answered "who is him?"

    filed = apply_review(review, settings)

    assert len(filed) == 3
    resolved = next(f for f in filed if f.text == "Talk to him.")
    assert resolved.topics == ["coach"]


def test_apply_review_records_audio_backref(settings):
    review = PendingReview(
        memo_name="2026-09-06",
        memo_date="2026-09-06",
        atoms=[Atom(text="Fix search.", topics=["voice-mind-backup"])],
        audio="20260906T101500-voice.ogg",
    )
    filed = apply_review(review, settings)

    assert filed[0].audio == "20260906T101500-voice.ogg", "must link back to the audio"
    page = (settings.topic_path / "voice-mind-backup.md").read_text(encoding="utf-8")
    assert "20260906T101500-voice.ogg" in page


def test_filed_atoms_are_searchable(settings):
    review = PendingReview(memo_name="2026-09-06", memo_date="2026-09-06", atoms=_atoms())
    apply_review(review, settings)

    assert len(search(settings, "grip")) == 1


# --- the M1 guarantee ------------------------------------------------------


@dataclass
class FakeMessage:
    """Records what the bot said, so we can assert on the user's experience."""

    texts: list = None
    message_id: int = 1

    def __post_init__(self):
        self.texts = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)
        return self

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)
        return self


def test_extraction_failure_still_confirms_saved(settings, monkeypatch, tmp_path):
    """A dead LLM must degrade the memo, never lose it or go silent."""
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)

    def _boom(*args, **kwargs):
        raise extract_mod.LLMError("model is down")

    monkeypatch.setattr(bot_mod, "extract_atoms", _boom)

    @dataclass
    class FakeTranscript:
        text: str = "some transcript"

    @dataclass
    class FakeResult:
        memo: object
        transcript: object
        archived_audio: object = None

    message = FakeMessage()

    class FakeUpdate:
        effective_message = message

    class FakeApp:
        bot_data: dict = {}

    class FakeContext:
        application = FakeApp()

    asyncio.run(
        bot_mod._offer_extraction(
            FakeUpdate(),
            FakeContext(),
            FakeResult(memo=memo, transcript=FakeTranscript()),
            settings,
        )
    )

    assert memo.path.is_file(), "the memo must survive an extraction failure"
    assert any("failed" in t for t in message.texts), "the user must be told, not left silent"
    assert not FakeApp.bot_data.get("reviews"), "no dangling review on failure"


def test_extraction_skipped_when_llm_unconfigured(tmp_path):
    """Without an LLM the bot behaves exactly as it did in M1: no extra noise."""
    settings = Settings(vault_path=tmp_path / "vault")
    message = FakeMessage()

    class FakeUpdate:
        effective_message = message

    class FakeContext:
        class application:
            bot_data: dict = {}

    asyncio.run(bot_mod._offer_extraction(FakeUpdate(), FakeContext(), object(), settings))

    assert message.texts == [], "unconfigured LLM must produce no messages at all"

"""Tests for the Telegram review layer.

Rendering and the bot handler only. What an approval *files* moved to
`test_proposal.py` along with the logic itself — this layer draws the message
and the buttons, nothing more.

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
from mindbackup.proposal import Proposal
from mindbackup.review import (
    CB_APPROVE,
    CB_DISCARD,
    CB_REVIEW,
    render_filed,
    render_review,
    review_keyboard,
)
from mindbackup.topics import iter_index
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
        Atom(
            text="Talk to him.", kind="todo", topics=[], confidence=0.2, ambiguity="Who is 'him'?"
        ),
    ]


def _proposal(atoms: list[Atom] | None = None) -> Proposal:
    return Proposal(
        memo_name="2026-09-06",
        memo_date="2026-09-06",
        extraction=Extraction(atoms=_atoms() if atoms is None else atoms, summary="s"),
    )


# --- rendering -------------------------------------------------------------


def test_render_review_flags_ambiguous_atoms():
    text = render_review(_proposal())

    assert "Fix search." in text
    assert "⚠️ 3. Talk to him." in text, "the unclear atom must be visibly flagged"
    assert "• 1. Fix search." in text
    assert "Who is 'him'?" in text, "the clarifying question must be shown"
    assert "⚠️ 1 unclear — tap 🔍" in text


def test_render_review_marks_each_atom_by_its_decision():
    proposal = _proposal()
    proposal.approve(0)
    proposal.reject(1)
    proposal.reassign(2, ["coach", "lower back"])

    text = render_review(proposal)

    assert "✅ 1. Fix search." in text
    assert "🗑 2. Buy grip." in text
    assert "🏷 3. Talk to him." in text
    assert "#coach #lower-back" in text, "a reassigned atom shows the topics the user chose"
    assert "Who is 'him'?" not in text, "a decided atom's question is answered"
    assert "unclear" not in text, "nothing is left to sort out"


def test_render_review_handles_empty_extraction():
    text = render_review(_proposal(atoms=[]))
    assert "Nothing worth extracting" in text


def test_render_review_truncates_long_lists():
    many = [Atom(text=f"Item {i}.", topics=["t"]) for i in range(30)]
    text = render_review(_proposal(atoms=many))
    assert "and 18 more" in text, "must not blow past Telegram's message limit"


# --- keyboard --------------------------------------------------------------


def _buttons(markup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


def test_review_keyboard_offers_approve_all_review_and_discard():
    buttons = _buttons(review_keyboard(_proposal()))

    assert [b.callback_data for b in buttons] == [CB_APPROVE, CB_REVIEW, CB_DISCARD]
    assert buttons[0].text == "✅ Approve all (3)", "unclear atoms are approved too"
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


def test_review_keyboard_counts_what_approve_all_would_file():
    proposal = _proposal()
    proposal.reject(0)
    proposal.reassign(2, ["coach"])

    buttons = _buttons(review_keyboard(proposal))

    assert buttons[0].text == "✅ Approve all (2)", (
        "one pending plus one reassigned; not the dropped one"
    )


def test_render_filed_handles_nothing_filed():
    assert render_filed([]) == "Nothing filed."


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

    monkeypatch.setattr(bot_mod, "propose_from_transcript", _boom)

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


# --- the review flow, end to end -------------------------------------------


@dataclass
class FakeQuery:
    """The half of `telegram.CallbackQuery` the review handler actually uses."""

    data: str
    message: FakeMessage
    texts: list = None
    toasts: list = None

    def __post_init__(self):
        self.texts = []
        self.toasts = []

    async def answer(self, text=None, **kwargs):
        self.toasts.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.texts.append(text)
        return self.message


def _run_review(settings, monkeypatch, memo, button: str):
    """Offer an extraction, then press one of its buttons. Returns what was said."""

    def _fake_propose(text, memo_name, memo_date, settings_, *, audio=None):
        return Proposal(
            memo_name=memo_name,
            memo_date=memo_date,
            extraction=Extraction(atoms=_atoms(), summary="s"),
            audio=audio,
        )

    monkeypatch.setattr(bot_mod, "propose_from_transcript", _fake_propose)
    # The keyboard needs the telegram package; the message text is what we assert on.
    monkeypatch.setattr(bot_mod, "review_keyboard", lambda proposal: None)

    message = FakeMessage()
    bot_data: dict = {"settings": settings}

    class FakeApp:
        pass

    FakeApp.bot_data = bot_data

    class FakeUser:
        id = 42

    class FakeUpdate:
        effective_message = message
        effective_user = FakeUser()
        callback_query = None

    class FakeContext:
        application = FakeApp()

    @dataclass
    class FakeTranscript:
        text: str = "some transcript"

    @dataclass
    class FakeResult:
        memo: object
        transcript: object
        archived_audio: object = None

    asyncio.run(
        bot_mod._offer_extraction(
            FakeUpdate(),
            FakeContext(),
            FakeResult(memo=memo, transcript=FakeTranscript()),
            settings,
        )
    )

    assert bot_data["reviews"][message.message_id], "the proposal must be kept to act on"

    query = FakeQuery(data=button, message=message)

    class FakeCallbackUpdate:
        effective_message = message
        effective_user = FakeUser()
        callback_query = query

    asyncio.run(bot_mod.handle_review_button(FakeCallbackUpdate(), FakeContext()))
    return query, bot_data


def test_approve_all_files_every_atom_unclear_ones_included(settings, monkeypatch):
    """Voice note -> ✅ -> vault. The unclear atom is filed with the model's topics,
    not held by a proposal that is about to be dropped."""
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)

    query, bot_data = _run_review(settings, monkeypatch, memo, CB_APPROVE)

    filed = list(iter_index(settings))
    assert {a.text for a in filed} == {"Fix search.", "Buy grip.", "Talk to him."}
    assert "Filed 3" in query.texts[-1], "the user must be told what happened"
    assert not bot_data["reviews"], "an actioned review must not linger"


@pytest.mark.parametrize("button", [CB_REVIEW, "mb:edit", "mb:nonsense"])
def test_a_button_that_does_not_file_writes_nothing_and_keeps_the_review(
    settings, monkeypatch, button
):
    """Unbuilt or unknown callback data must never fall through to filing."""
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)

    query, bot_data = _run_review(settings, monkeypatch, memo, button)

    assert list(iter_index(settings)) == []
    assert query.toasts and query.toasts[-1], "the tap must be answered with a toast"
    assert bot_data["reviews"], "the review is still there to act on"


def test_discarding_writes_nothing(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)

    query, bot_data = _run_review(settings, monkeypatch, memo, CB_DISCARD)

    assert list(iter_index(settings)) == [], "discard must not reach the vault"
    assert "Discarded" in query.texts[-1]
    assert memo.path.is_file(), "the transcript survives a discarded extraction"
    assert not bot_data["reviews"]

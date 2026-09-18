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
import itertools
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import ForceReply

from mindbackup import bot as bot_mod
from mindbackup import extract as extract_mod
from mindbackup.browse import TOPIC_TOKEN_BYTES, resolve_topic
from mindbackup.config import Settings
from mindbackup.extract import Atom, Extraction
from mindbackup.proposal import Proposal
from mindbackup.review import (
    CB_APPROVE,
    CB_DISCARD,
    CB_DROP,
    CB_KEEP,
    CB_OVERVIEW,
    CB_PICK,
    CB_PICK_NEW,
    CB_PICK_PAGE,
    CB_PICK_TOPIC,
    CB_REVIEW,
    MAX_ATOMS_SHOWN,
    picker_token_bytes,
    render_filed,
    render_review,
    review_keyboard,
    step_keyboard,
    topic_picker_keyboard,
)
from mindbackup.topics import file_atoms, iter_index, topic_slug
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
    """The half of `telegram.CallbackQuery` the review handler actually uses.

    Reused across taps, so `texts` is everything the review message showed,
    in order, and `markups` the keyboards that came with it.
    """

    data: str
    message: FakeMessage
    texts: list = None
    markups: list = None
    toasts: list = None

    def __post_init__(self):
        self.texts = []
        self.markups = []
        self.toasts = []

    async def answer(self, text=None, **kwargs):
        self.toasts.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return self.message


def _start_conversation(settings, monkeypatch, memo, atoms: list[Atom] | None = None):
    """Offer an extraction. Returns `tap(data)`, which presses a button on it;
    `reply(text, to)`, which sends a text reply to message id `to`; the query,
    whose `texts`/`markups` are what the review message showed and whose
    `sent` is every new message the bot sent after the offer; and `bot_data`.
    """

    def _fake_propose(text, memo_name, memo_date, settings_, *, audio=None):
        return Proposal(
            memo_name=memo_name,
            memo_date=memo_date,
            extraction=Extraction(atoms=_atoms() if atoms is None else atoms, summary="s"),
            audio=audio,
        )

    monkeypatch.setattr(bot_mod, "propose_from_transcript", _fake_propose)

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

    query = FakeQuery(data="", message=message)

    class FakeCallbackUpdate:
        effective_message = message
        effective_user = FakeUser()
        callback_query = query

    def tap(data: str) -> None:
        query.data = data
        asyncio.run(bot_mod.handle_review_button(FakeCallbackUpdate(), FakeContext()))

    # From here on, a reply is a new message with its own id, like Telegram's.
    query.sent = []
    ids = itertools.count(message.message_id + 1)

    async def _send(text, reply_markup=None, **kwargs):
        sent = FakeMessage(message_id=next(ids))
        query.sent.append((sent.message_id, text, reply_markup))
        return sent

    message.reply_text = _send

    class FakeBot:
        async def edit_message_text(
            self, text, chat_id=None, message_id=None, reply_markup=None, **kwargs
        ):
            assert message_id == message.message_id, "only the review message is edited"
            query.texts.append(text)
            query.markups.append(reply_markup)

    FakeContext.bot = FakeBot()

    def reply(text: str, to: int | None) -> None:
        incoming = SimpleNamespace(
            text=text,
            chat_id=7,
            reply_to_message=SimpleNamespace(message_id=to) if to is not None else None,
            reply_text=_send,
        )

        class FakeReplyUpdate:
            effective_message = incoming
            effective_user = FakeUser()
            callback_query = None

        asyncio.run(bot_mod.handle_topic_reply(FakeReplyUpdate(), FakeContext()))

    return tap, reply, query, bot_data


def _start_review(settings, monkeypatch, memo, atoms: list[Atom] | None = None):
    """`_start_conversation` for tests that only press buttons."""
    tap, _, query, bot_data = _start_conversation(settings, monkeypatch, memo, atoms)
    return tap, query, bot_data


@dataclass
class Reply:
    """A `_run_review` step: a text reply to the latest ✍️ prompt."""

    text: str


def _run_review(settings, monkeypatch, memo, *steps: str | Reply):
    """Offer an extraction, then press its buttons and send its replies in order.
    Returns what was said."""
    tap, reply, query, bot_data = _start_conversation(settings, monkeypatch, memo)
    for step in steps:
        if isinstance(step, Reply):
            reply(step.text, to=_prompt_id(query))
        else:
            tap(step)
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


@pytest.mark.parametrize("button", ["mb:edit", "mb:nonsense"])
def test_a_button_that_does_not_file_writes_nothing_and_keeps_the_review(
    settings, monkeypatch, button
):
    """Unknown callback data must never fall through to filing."""
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


# --- one-by-one review -----------------------------------------------------


def test_review_starts_at_the_first_atom(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)

    query, _ = _run_review(settings, monkeypatch, memo, CB_REVIEW)

    assert "1 of 3" in query.texts[-1]
    assert "Fix search." in query.texts[-1]
    assert [b.callback_data for b in _buttons(query.markups[-1])] == [
        f"{CB_KEEP}0",
        f"{CB_DROP}0",
        f"{CB_PICK}0",
        CB_OVERVIEW,
    ]


def test_review_shows_the_models_question(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)

    query, _ = _run_review(settings, monkeypatch, memo, CB_REVIEW, f"{CB_KEEP}0", f"{CB_KEEP}1")

    assert "3 of 3" in query.texts[-1]
    assert "Who is 'him'?" in query.texts[-1]


def test_dropping_one_and_keeping_the_rest_files_only_the_kept(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)

    query, bot_data = _run_review(
        settings, monkeypatch, memo, CB_REVIEW, f"{CB_KEEP}0", f"{CB_DROP}1", f"{CB_KEEP}2"
    )

    assert {a.text for a in iter_index(settings)} == {"Fix search.", "Talk to him."}
    assert "Filed 2" in query.texts[-1]
    assert not bot_data["reviews"], "a finished review must not linger"


def test_nothing_is_written_until_the_last_decision(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    commits = []
    real_commit = Proposal.commit

    def _counting_commit(self, settings_):
        commits.append(self.memo_name)
        return real_commit(self, settings_)

    monkeypatch.setattr(Proposal, "commit", _counting_commit)
    tap, _, _ = _start_review(settings, monkeypatch, memo)

    for data in (CB_REVIEW, f"{CB_KEEP}0", f"{CB_DROP}1"):
        tap(data)
        assert list(iter_index(settings)) == [], f"nothing may be filed after {data}"

    tap(f"{CB_KEEP}2")

    assert {a.text for a in iter_index(settings)} == {"Fix search.", "Talk to him."}
    assert len(commits) == 1, "commit runs once, at the end"


def test_back_then_approve_all_keeps_the_decisions_made(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    commits = []
    real_commit = Proposal.commit

    def _counting_commit(self, settings_):
        commits.append(self.memo_name)
        return real_commit(self, settings_)

    monkeypatch.setattr(Proposal, "commit", _counting_commit)
    tap, query, bot_data = _start_review(settings, monkeypatch, memo)

    tap(CB_REVIEW)
    tap(f"{CB_DROP}0")
    tap(CB_OVERVIEW)

    assert "🗑 1. Fix search." in query.texts[-1], "the overview shows what was decided"
    assert _buttons(query.markups[-1])[0].text == "✅ Approve all (2)"
    assert list(iter_index(settings)) == []

    tap(CB_APPROVE)

    assert {a.text for a in iter_index(settings)} == {"Buy grip.", "Talk to him."}
    assert len(commits) == 1
    assert not bot_data["reviews"]


def test_review_reaches_atoms_past_the_overview_cap(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    many = [Atom(text=f"Item {i}.", topics=["t"]) for i in range(MAX_ATOMS_SHOWN + 2)]
    tap, query, _ = _start_review(settings, monkeypatch, memo, atoms=many)

    tap(CB_REVIEW)
    for index in range(len(many) - 1):
        tap(f"{CB_KEEP}{index}")

    last = len(many) - 1
    assert f"{last + 1} of {len(many)}" in query.texts[-1]
    assert f"Item {last}." in query.texts[-1]

    tap(f"{CB_KEEP}{last}")

    assert len(list(iter_index(settings))) == len(many)


@pytest.mark.parametrize(
    "data",
    [f"{CB_KEEP}0", f"{CB_DROP}0", f"{CB_KEEP}99", f"{CB_KEEP}-1", f"{CB_DROP}x", CB_KEEP],
)
def test_a_double_tap_or_stale_index_is_answered_and_changes_nothing(settings, monkeypatch, data):
    """Index 0 is already kept by the time `data` is tapped."""
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, query, bot_data = _start_review(settings, monkeypatch, memo)
    tap(CB_REVIEW)
    tap(f"{CB_KEEP}0")
    shown = list(query.texts)

    tap(data)

    assert query.toasts[-1], "the tap must be answered with a toast"
    assert query.texts == shown, "the message must not change"
    proposal = bot_data["reviews"][1]
    assert set(proposal.decisions) == {0}
    assert list(iter_index(settings)) == []


def test_step_keyboard_fits_telegrams_callback_limit():
    buttons = _buttons(step_keyboard(10**6))
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


# --- topic picker ----------------------------------------------------------


def _seed_topics(settings, topics: list[str]) -> None:
    """Put topics in the vault from another memo, so the picker has something to offer."""
    file_atoms(
        [Atom(text=f"Seed {i}.", topics=[t]) for i, t in enumerate(topics)],
        "2026-09-01",
        "2026-09-01",
        settings,
    )


def _this_memo(settings) -> dict[str, list[str]]:
    return {a.text: a.topics for a in iter_index(settings) if a.memo == "2026-09-06"}


def _button(markup, text: str):
    return next(b for b in _buttons(markup) if b.text == text)


def test_picking_a_known_topic_files_under_it(settings, monkeypatch):
    _seed_topics(settings, ["gym", "padel"])
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, query, bot_data = _start_review(settings, monkeypatch, memo)

    tap(CB_REVIEW)
    tap(f"{CB_PICK}0")

    assert "Pick a topic" in query.texts[-1]
    assert "Fix search." in query.texts[-1], "the picker says which atom it is for"
    tap(_button(query.markups[-1], "gym").callback_data)

    assert "2 of 3" in query.texts[-1], "picking a topic moves on to the next atom"
    assert _this_memo(settings) == {}, "nothing filed before the last decision"

    tap(f"{CB_KEEP}1")
    tap(f"{CB_KEEP}2")

    filed = _this_memo(settings)
    assert filed["Fix search."] == ["gym"], "filed under the chosen topic, not the model's"
    assert filed["Buy grip."] == ["padel"]
    assert not bot_data["reviews"]


def test_back_from_the_picker_returns_to_the_same_atom(settings, monkeypatch):
    _seed_topics(settings, ["gym"])
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)

    query, bot_data = _run_review(
        settings, monkeypatch, memo, CB_REVIEW, f"{CB_KEEP}0", f"{CB_PICK}1", CB_REVIEW
    )

    assert "2 of 3" in query.texts[-1]
    assert "Pick a topic" not in query.texts[-1]
    assert set(bot_data["reviews"][1].decisions) == {0}, "backing out decides nothing"


def test_picker_pages_through_known_topics(settings, monkeypatch):
    _seed_topics(settings, [f"topic-{i:02d}" for i in range(20)])
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, query, _ = _start_review(settings, monkeypatch, memo)

    tap(CB_REVIEW)
    tap(f"{CB_PICK}0")
    first = [b.text for b in _buttons(query.markups[-1])]
    assert "page 1 of 3" in query.texts[-1]

    tap(_button(query.markups[-1], "▶️").callback_data)

    assert "page 2 of 3" in query.texts[-1]
    second = [b.text for b in _buttons(query.markups[-1])]
    assert not {t for t in first if t.startswith("topic-")} & set(second)
    assert "◀️" in second and "▶️" in second


def test_picker_with_no_known_topics_offers_a_new_one_and_back(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)

    query, _ = _run_review(settings, monkeypatch, memo, CB_REVIEW, f"{CB_PICK}0")

    assert "No topics" in query.texts[-1]
    assert [b.callback_data for b in _buttons(query.markups[-1])] == [f"{CB_PICK_NEW}0", CB_REVIEW]


@pytest.mark.parametrize(
    "data",
    [
        f"{CB_PICK_TOPIC}1:gone-topic",
        f"{CB_PICK_TOPIC}2:",
        f"{CB_PICK_TOPIC}0:gym",
        f"{CB_PICK}0",
        f"{CB_PICK}9",
        f"{CB_PICK_PAGE}x:1",
    ],
)
def test_a_stale_picker_button_is_answered_and_changes_nothing(settings, monkeypatch, data):
    """Index 0 is already kept; "gone-topic" is not in the vault."""
    _seed_topics(settings, ["gym"])
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, query, bot_data = _start_review(settings, monkeypatch, memo)
    tap(CB_REVIEW)
    tap(f"{CB_KEEP}0")
    shown = list(query.texts)

    tap(data)

    assert query.toasts[-1], "the tap must be answered with a toast"
    assert query.texts == shown
    assert set(bot_data["reviews"][1].decisions) == {0}


def test_picker_callback_data_fits_telegrams_limit_on_every_page():
    """A long slug, and one that fits /get_topic's budget but not the picker's."""
    huge = "a very long topic name " * 10
    borderline = "x" * TOPIC_TOKEN_BYTES
    assert len(topic_slug(borderline).encode()) > picker_token_bytes(12)
    topics = [huge, borderline] + [f"topic-{i:02d}" for i in range(20)]

    for index in (0, 12, 10**6):
        for page in range(3):
            for button in _buttons(topic_picker_keyboard(index, topics, page)):
                assert len(button.callback_data.encode()) <= 64, button.callback_data

    for topic in (huge, borderline):
        button = _button(topic_picker_keyboard(12, topics, 0), topic)
        token = button.callback_data.split(":", 3)[3]
        assert resolve_topic(token, topics, picker_token_bytes(12)) == topic


# --- new topics by text reply ----------------------------------------------


def _prompt_id(query) -> int:
    """The id of the latest ✍️ prompt the bot sent."""
    return next(mid for mid, text, _ in reversed(query.sent) if "comma-separated" in text)


def test_new_topic_button_asks_with_a_force_reply(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, _, query, bot_data = _start_conversation(settings, monkeypatch, memo)

    tap(CB_REVIEW)
    tap(f"{CB_PICK}0")
    tap(f"{CB_PICK_NEW}0")

    _, text, markup = query.sent[-1]
    assert text == "✍️ Topics for #1, comma-separated:"
    assert isinstance(markup, ForceReply)
    assert bot_data["topic_prompts"] == {_prompt_id(query): (1, 0)}


def test_replying_with_new_topics_files_under_all_of_them(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, reply, query, bot_data = _start_conversation(settings, monkeypatch, memo)
    tap(CB_REVIEW)
    tap(f"{CB_PICK}0")
    tap(f"{CB_PICK_NEW}0")

    reply("gym, Health", to=_prompt_id(query))

    assert "2 of 3" in query.texts[-1], "the review message moves on to the next atom"
    assert query.sent[-1][1] == "🏷 #1 → #gym #health"
    assert bot_data["topic_prompts"] == {}, "an answered prompt is forgotten"
    assert list(iter_index(settings)) == [], "nothing filed before the last decision"

    tap(f"{CB_KEEP}1")
    tap(f"{CB_KEEP}2")

    filed = {a.text: a.topics for a in iter_index(settings)}
    assert filed["Fix search."] == ["gym", "health"]


def test_a_reply_on_the_last_atom_commits(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, reply, query, bot_data = _start_conversation(settings, monkeypatch, memo)
    for data in (CB_REVIEW, f"{CB_KEEP}0", f"{CB_KEEP}1", f"{CB_PICK}2", f"{CB_PICK_NEW}2"):
        tap(data)

    reply("coach", to=_prompt_id(query))

    assert "Filed 3" in query.texts[-1]
    assert {a.text: a.topics for a in iter_index(settings)}["Talk to him."] == ["coach"]
    assert not bot_data["reviews"]


@pytest.mark.parametrize("text", ["", "  ", " , #, "])
def test_an_empty_reply_asks_again(settings, monkeypatch, text):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, reply, query, bot_data = _start_conversation(settings, monkeypatch, memo)
    tap(CB_REVIEW)
    tap(f"{CB_PICK}0")
    tap(f"{CB_PICK_NEW}0")
    first = _prompt_id(query)

    reply(text, to=first)

    second = _prompt_id(query)
    assert second != first
    assert "no topics" in query.sent[-1][1]
    assert isinstance(query.sent[-1][2], ForceReply)
    assert bot_data["topic_prompts"] == {second: (1, 0)}
    assert bot_data["reviews"][1].decisions == {}, "no reassigning to nothing"


def test_typed_topics_are_escaped_for_markdown(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, reply, query, _ = _start_conversation(settings, monkeypatch, memo)
    tap(CB_REVIEW)
    tap(f"{CB_PICK}0")
    tap(f"{CB_PICK_NEW}0")

    reply("my_topic*", to=_prompt_id(query))
    tap(CB_OVERVIEW)

    assert query.sent[-1][1] == "🏷 #1 → #my\\_topic\\*"
    assert "#my\\_topic\\*" in query.texts[-1], "the overview shows them escaped too"


@pytest.mark.parametrize("to", [999, None])
def test_a_reply_to_an_unknown_prompt_gets_the_usual_answer(settings, monkeypatch, to):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    _, reply, query, bot_data = _start_conversation(settings, monkeypatch, memo)

    reply("gym", to=to)

    assert query.sent[-1][1] == bot_mod.NOT_A_VOICE_NOTE
    assert bot_data["reviews"][1].decisions == {}


def test_a_reply_after_the_atom_was_decided_by_button(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, reply, query, bot_data = _start_conversation(settings, monkeypatch, memo)
    tap(CB_REVIEW)
    tap(f"{CB_PICK}0")
    tap(f"{CB_PICK_NEW}0")
    prompt = _prompt_id(query)
    tap(CB_REVIEW)
    tap(f"{CB_DROP}0")
    shown = list(query.texts)

    reply("gym", to=prompt)

    assert "already decided" in query.sent[-1][1]
    assert query.texts == shown
    assert bot_data["reviews"][1].decisions[0].verdict.value == "reject"


def test_a_reply_after_the_review_was_discarded(settings, monkeypatch):
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)
    tap, reply, query, _ = _start_conversation(settings, monkeypatch, memo)
    tap(CB_REVIEW)
    tap(f"{CB_PICK}0")
    tap(f"{CB_PICK_NEW}0")
    prompt = _prompt_id(query)
    tap(CB_DISCARD)

    reply("gym", to=prompt)

    assert "expired" in query.sent[-1][1]
    assert list(iter_index(settings)) == []


def test_a_review_mixing_every_kind_of_decision(settings, monkeypatch):
    """The manual pass: drop one, pick a known topic for one, type new topics for one."""
    _seed_topics(settings, ["gym"])
    memo = write_memo("some transcript", date(2026, 9, 6), settings.memo_path)

    query, bot_data = _run_review(
        settings,
        monkeypatch,
        memo,
        CB_REVIEW,
        f"{CB_DROP}0",
        f"{CB_PICK}1",
        f"{CB_PICK_TOPIC}1:gym",
        f"{CB_PICK}2",
        f"{CB_PICK_NEW}2",
        Reply("coach, lower back"),
    )

    assert _this_memo(settings) == {
        "Buy grip.": ["gym"],
        "Talk to him.": ["coach", "lower back"],
    }
    assert "Filed 2" in query.texts[-1]
    assert not bot_data["reviews"]
    assert not bot_data["topic_prompts"]

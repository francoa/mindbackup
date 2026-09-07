"""Tests for `/get_topic` — the browse side of the bot.

The rules worth pinning down: the user is shown the *page* (so Obsidian edits
survive the round trip), a page too long for Telegram is split rather than
truncated, and a button still resolves after the bot has restarted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mindbackup import bot as bot_mod
from mindbackup.browse import (
    CB_PAGE,
    CB_TOPIC,
    MAX_MESSAGE_CHARS,
    TOPICS_PER_PAGE,
    chunk_message,
    page_count,
    page_slice,
    render_topic_list,
    resolve_topic,
    topic_body,
    topic_token,
)
from mindbackup.config import Settings
from mindbackup.extract import Atom
from mindbackup.topics import file_atoms, known_topics


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(vault_path=tmp_path / "vault", allowed_users=frozenset({42}))


@pytest.fixture
def filled(settings: Settings) -> Settings:
    file_atoms(
        [
            Atom(text="Fix search.", kind="todo", topics=["voice-mind-backup"]),
            Atom(text="Ship M2.", kind="todo", topics=["voice-mind-backup"]),
            Atom(text="Buy grip.", kind="todo", topics=["padel"]),
        ],
        "2026-09-06",
        "2026-09-06",
        settings,
    )
    return settings


# --- tokens ----------------------------------------------------------------


def test_token_round_trips_through_the_topic_list():
    topics = ["voice mind backup", "padel"]
    for topic in topics:
        assert resolve_topic(topic_token(topic), topics) == topic


def test_token_stays_inside_telegram_callback_limit():
    huge = "a very long topic name " * 10
    token = topic_token(huge)
    assert len(f"{CB_TOPIC}{token}".encode("utf-8")) <= 64


def test_long_topic_token_is_deterministic():
    """A restart must not orphan the buttons already on screen."""
    huge = "a very long topic name " * 10
    assert topic_token(huge) == topic_token(huge)
    assert resolve_topic(topic_token(huge), [huge]) == huge


def test_resolve_accepts_a_name_the_user_typed():
    topics = ["voice mind backup"]
    assert resolve_topic("Voice Mind Backup", topics) == "voice mind backup"
    assert resolve_topic("voice-mind-backup", topics) == "voice mind backup"
    assert resolve_topic("padel", topics) is None


# --- listing ---------------------------------------------------------------


def test_empty_vault_says_so_rather_than_showing_nothing():
    assert "No topics yet" in render_topic_list([])


def test_pages_cover_every_topic_exactly_once():
    topics = [f"topic-{i}" for i in range(TOPICS_PER_PAGE * 2 + 3)]
    seen = [t for page in range(page_count(topics)) for t in page_slice(topics, page)]
    assert seen == topics


def test_out_of_range_page_is_clamped():
    topics = ["a", "b"]
    assert page_slice(topics, 99) == topics
    assert page_slice(topics, -5) == topics


# --- content ---------------------------------------------------------------


def test_topic_body_shows_the_page_not_the_index(filled):
    body = topic_body("voice-mind-backup", filled)

    assert "Fix search." in body
    assert "Ship M2." in body
    assert "Buy grip." not in body, "other topics must not leak in"
    assert "type: topic" not in body, "frontmatter is Obsidian machinery"
    assert "^mb-" not in body, "block references are noise on a phone"


def test_hand_edits_to_the_page_are_shown(filled):
    page = filled.topic_path / "voice-mind-backup.md"
    page.write_text(page.read_text(encoding="utf-8") + "- typed by hand\n", encoding="utf-8")

    assert "typed by hand" in topic_body("voice-mind-backup", filled)


def test_topic_body_falls_back_to_the_index_when_the_page_is_gone(filled):
    (filled.topic_path / "padel.md").unlink()

    body = topic_body("padel", filled)
    assert "Buy grip." in body, "a deleted page must not hide the filed atoms"


def test_unknown_topic_has_no_body(settings):
    assert topic_body("nothing-here", settings) == ""


# --- chunking --------------------------------------------------------------


def test_chunking_preserves_every_line():
    text = "\n".join(f"- line {i} " + "x" * 80 for i in range(200))
    parts = chunk_message(text)

    assert len(parts) > 1, "this page is past Telegram's limit"
    assert all(len(part) <= MAX_MESSAGE_CHARS for part in parts)
    assert "\n".join(parts) == text, "nothing may be lost in the split"


def test_a_single_overlong_line_is_split_not_dropped():
    parts = chunk_message("y" * (MAX_MESSAGE_CHARS * 2 + 10))

    assert all(len(part) <= MAX_MESSAGE_CHARS for part in parts)
    assert "".join(parts) == "y" * (MAX_MESSAGE_CHARS * 2 + 10)


def test_short_page_is_one_message():
    assert chunk_message("- just the one line") == ["- just the one line"]


# --- the handlers ----------------------------------------------------------


@dataclass
class FakeMessage:
    """Records what the bot said, so we can assert on the user's experience."""

    texts: list = field(default_factory=list)
    markups: list = field(default_factory=list)
    message_id: int = 1

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return self

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return self


def _context(settings: Settings, args: list[str] | None = None):
    class FakeApp:
        bot_data = {"settings": settings}

    class FakeContext:
        application = FakeApp()

    FakeContext.args = args or []
    return FakeContext()


def _update(message: FakeMessage, user_id: int = 42, query=None):
    class FakeUser:
        id = user_id

    class FakeUpdate:
        effective_user = FakeUser()
        effective_message = message
        callback_query = query

    return FakeUpdate()


def test_get_topic_lists_every_topic(filled):
    message = FakeMessage()
    asyncio.run(bot_mod.cmd_get_topic(_update(message), _context(filled)))

    assert "2 topic(s)" in message.texts[-1]
    labels = [
        button.text
        for row in message.markups[-1].inline_keyboard
        for button in row
    ]
    assert any(label.startswith("voice-mind-backup") for label in labels)
    assert any(label.startswith("padel") for label in labels)


def test_get_topic_with_a_name_shows_the_content_directly(filled):
    message = FakeMessage()
    asyncio.run(bot_mod.cmd_get_topic(_update(message), _context(filled, ["padel"])))

    assert "Buy grip." in "\n".join(message.texts)


def test_get_topic_with_an_unknown_name_falls_back_to_the_list(filled):
    message = FakeMessage()
    asyncio.run(bot_mod.cmd_get_topic(_update(message), _context(filled, ["nope"])))

    assert "No topic matching" in message.texts[0]
    assert "2 topic(s)" in message.texts[-1], "the user is not left at a dead end"


def test_get_topic_is_refused_for_unauthorised_users(filled):
    message = FakeMessage()
    asyncio.run(bot_mod.cmd_get_topic(_update(message, user_id=9), _context(filled)))

    assert "allowlist" in message.texts[0]
    assert not any("Buy grip." in text for text in message.texts)


@dataclass
class FakeQuery:
    data: str
    message: FakeMessage
    answered: bool = False

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        return await self.message.edit_text(text, **kwargs)


def test_pressing_a_topic_button_shows_its_content(filled):
    message = FakeMessage()
    topic = "voice-mind-backup"
    query = FakeQuery(data=f"{CB_TOPIC}{topic_token(topic)}", message=message)

    asyncio.run(bot_mod.handle_topic_button(_update(message, query=query), _context(filled)))

    assert query.answered, "Telegram spins forever without an answer()"
    assert "Fix search." in "\n".join(message.texts)


def test_pressing_a_button_for_a_deleted_topic_explains_itself(filled):
    message = FakeMessage()
    query = FakeQuery(data=f"{CB_TOPIC}long-gone", message=message)

    asyncio.run(bot_mod.handle_topic_button(_update(message, query=query), _context(filled)))

    assert "gone" in message.texts[-1]


def test_paging_button_redraws_the_list(settings):
    file_atoms(
        [Atom(text=f"Item {i}.", topics=[f"topic-{i:02d}"]) for i in range(20)],
        "2026-09-06",
        "2026-09-06",
        settings,
    )
    message = FakeMessage()
    query = FakeQuery(data=f"{CB_PAGE}1", message=message)

    asyncio.run(bot_mod.handle_topic_button(_update(message, query=query), _context(settings)))

    assert "page 2 of" in message.texts[-1]
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    expected = page_slice(known_topics(settings), 1)
    assert all(any(label.startswith(t) for label in labels) for t in expected)

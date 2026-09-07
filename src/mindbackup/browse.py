"""Telegram browse UI for topic pages — the read side of `/get_topic`.

Split from bot.py for the same reason review.py is: the handler stays a thin
wrapper and everything worth asserting on (which topics are offered, what a
button carries, how a long page is cut up) is testable without a Telegram
server.

What the user sees is the topic *page*, not a re-render of the atom index.
The page is the human-facing artefact and it is the thing they may have
hand-edited in Obsidian; showing anything else would quietly hide their edits.
The index is only a fallback for a topic whose page is missing.
"""

from __future__ import annotations

import hashlib
import logging
import re

from .config import Settings
from .extract import normalise_topic
from .topics import search, topic_page_path, topic_slug

logger = logging.getLogger(__name__)

TOPICS_PER_PAGE = 8
#: Telegram rejects a message over 4096 characters; leave room for the header.
MAX_MESSAGE_CHARS = 3500
#: Telegram rejects callback_data over 64 bytes; "mbt:t:" eats six of them.
MAX_TOKEN_BYTES = 58
#: Cap on the fallback index render, so a huge topic cannot spam the chat.
MAX_FALLBACK_ATOMS = 200

CB_TOPIC = "mbt:t:"
CB_PAGE = "mbt:p:"
CB_LIST = "mbt:list"

_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_BLOCK_REF_RE = re.compile(r"[ \t]*\^mb-[0-9a-f]{10}[ \t]*$", re.MULTILINE)


def topic_token(topic: str) -> str:
    """Callback token for a topic: its slug, or a hash when the slug is too long.

    The hash is derived from the slug rather than handed out from a table in
    process memory, so a button still works after the bot restarts — the same
    trade the block references make on the page itself.
    """
    slug = topic_slug(topic)
    if len(slug.encode("utf-8")) <= MAX_TOKEN_BYTES:
        return slug
    return "#" + hashlib.sha256(slug.encode("utf-8")).hexdigest()[:10]


def resolve_topic(token: str, topics: list[str]) -> str | None:
    """Token (or a name the user typed) back to one of the known topics."""
    token = (token or "").strip()
    if not token:
        return None
    for topic in topics:
        if topic_token(topic) == token:
            return topic
    wanted = normalise_topic(token)
    for topic in topics:
        if normalise_topic(topic) == wanted or topic_slug(topic) == topic_slug(token):
            return topic
    return None


def page_count(topics: list[str]) -> int:
    return max(1, -(-len(topics) // TOPICS_PER_PAGE))


def _clamp_page(topics: list[str], page: int) -> int:
    return max(0, min(page, page_count(topics) - 1))


def page_slice(topics: list[str], page: int) -> list[str]:
    page = _clamp_page(topics, page)
    start = page * TOPICS_PER_PAGE
    return topics[start : start + TOPICS_PER_PAGE]


def render_topic_list(topics: list[str], page: int = 0) -> str:
    """The message body above the topic buttons."""
    if not topics:
        return (
            "📚 No topics yet.\n\n"
            "Send a voice note, or run `mindbackup extract`, and the topics "
            "will show up here."
        )
    total_pages = page_count(topics)
    header = f"📚 {len(topics)} topic(s) — pick one:"
    if total_pages > 1:
        header += f"\n(page {_clamp_page(topics, page) + 1} of {total_pages})"
    return header


def topics_keyboard(topics: list[str], page: int = 0, counts: dict | None = None):
    """One button per topic, plus prev/next when there are too many for a screen.

    Imported lazily so the rendering above stays testable without telegram.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    page = _clamp_page(topics, page)
    rows = []
    for topic in page_slice(topics, page):
        count = (counts or {}).get(topic)
        label = topic if count is None else f"{topic} ({count})"
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"{CB_TOPIC}{topic_token(topic)}")]
        )

    total_pages = page_count(topics)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"{CB_PAGE}{page - 1}"))
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=CB_LIST)
        )
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"{CB_PAGE}{page + 1}"))
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


def back_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ All topics", callback_data=CB_LIST)]]
    )


def _strip_page_furniture(text: str) -> str:
    """Drop what only means something inside Obsidian: frontmatter, block refs."""
    text = _FRONTMATTER_RE.sub("", text, count=1)
    text = _BLOCK_REF_RE.sub("", text)
    return text.strip()


def _from_index(topic: str, settings: Settings) -> str:
    """Fallback render for a topic with no page — straight from the atom index."""
    stored = search(settings, topic=topic, limit=MAX_FALLBACK_ATOMS)
    if not stored:
        return ""
    lines = [f"# {topic}", ""]
    for atom in stored:
        kind = "" if atom.kind == "fact" else f"{atom.kind}: "
        lines.append(f"- {kind}{atom.text} — {atom.memo_date} [[{atom.memo}]]")
    if len(stored) == MAX_FALLBACK_ATOMS:
        lines.append(f"…only the {MAX_FALLBACK_ATOMS} most recent are shown.")
    return "\n".join(lines)


def topic_body(topic: str, settings: Settings) -> str:
    """Everything filed under `topic`, ready to send. Empty string if nothing."""
    path = topic_page_path(topic, settings)
    if path.is_file():
        try:
            content = _strip_page_furniture(path.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("Could not read topic page %s: %s", path, exc)
            return f"⚠️ Could not read {path.name}: {exc}"
        if content:
            return content
    return _from_index(topic, settings)


def chunk_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Cut a topic page into Telegram-sized messages, on line boundaries.

    A single line longer than the limit is hard-split rather than dropped —
    losing content to make it fit would be the wrong failure.
    """
    if not text:
        return []

    chunks: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            chunks.append("\n".join(current))
            current, size = [], 0

    for line in text.split("\n"):
        while len(line) > limit:
            flush()
            chunks.append(line[:limit])
            line = line[limit:]
        cost = len(line) + (1 if current else 0)
        if size + cost > limit:
            flush()
            cost = len(line)
        current.append(line)
        size += cost

    flush()
    return chunks


__all__ = [
    "CB_LIST",
    "CB_PAGE",
    "CB_TOPIC",
    "TOPICS_PER_PAGE",
    "back_keyboard",
    "chunk_message",
    "page_count",
    "page_slice",
    "render_topic_list",
    "resolve_topic",
    "topic_body",
    "topic_token",
    "topics_keyboard",
]

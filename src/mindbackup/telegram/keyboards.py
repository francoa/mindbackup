"""Inline keyboards and the callback data they carry.

Everything here exists because of Telegram's button model: callback_data is
capped at 64 bytes, hence tokens, budgets and short prefixes. The keyboards
import telegram lazily so what they carry stays testable without it.
"""

from __future__ import annotations

import hashlib

from ..proposal import Proposal
from ..topic_view import _clamp_page, page_count, page_slice, resolve_topic
from ..topics import topic_slug

#: Telegram rejects callback_data over 64 bytes.
MAX_CALLBACK_BYTES = 64

# /get_topic browse.
CB_TOPIC = "mbt:t:"
CB_PAGE = "mbt:p:"
CB_LIST = "mbt:list"
#: What is left of the callback limit for a token after "mbt:t:".
TOPIC_TOKEN_BYTES = MAX_CALLBACK_BYTES - len(CB_TOPIC)

# Review of extracted atoms.
CB_APPROVE = "mb:ok"
CB_DISCARD = "mb:no"
CB_REVIEW = "mb:w"
CB_OVERVIEW = "mb:o"
CB_KEEP = "mb:a:"
CB_DROP = "mb:r:"
CB_PICK = "mb:t:"
CB_PICK_TOPIC = "mb:ts:"
CB_PICK_PAGE = "mb:tp:"
CB_PICK_NEW = "mb:tn:"


def topic_token(topic: str, max_bytes: int) -> str:
    """Callback token for a topic: its slug, or a hash when the slug is over `max_bytes`.

    `max_bytes` is what the caller's prefix leaves of Telegram's 64-byte
    limit, so each keyboard passes its own. The hash is 11 bytes, so the
    budget must be at least that.

    The hash is derived from the slug rather than handed out from a table in
    process memory, so a button still works after the bot restarts — the same
    trade the block references make on the page itself.
    """
    slug = topic_slug(topic)
    if len(slug.encode("utf-8")) <= max_bytes:
        return slug
    return "#" + hashlib.sha256(slug.encode("utf-8")).hexdigest()[:10]


def resolve_token(token: str, topics: list[str], max_bytes: int) -> str | None:
    """Token (or a name the user typed) back to one of the known topics.

    `max_bytes` must be the budget the token was made with. A token match
    wins; anything else is treated as a name via `topic_view.resolve_topic`.
    """
    token = (token or "").strip()
    if not token:
        return None
    for topic in topics:
        if topic_token(topic, max_bytes) == token:
            return topic
    return resolve_topic(token, topics)


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
            [
                InlineKeyboardButton(
                    label, callback_data=f"{CB_TOPIC}{topic_token(topic, TOPIC_TOKEN_BYTES)}"
                )
            ]
        )

    total_pages = page_count(topics)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"{CB_PAGE}{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=CB_LIST))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"{CB_PAGE}{page + 1}"))
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


def back_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ All topics", callback_data=CB_LIST)]])


def review_keyboard(proposal: Proposal):
    """Approve all / review one by one / discard. Imported lazily so tests need no telegram."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # What ✅ would file: everything still pending plus what is already approved.
    filed = len(proposal.pending()) + len(proposal.approved())
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"✅ Approve all ({filed})", callback_data=CB_APPROVE)],
            [InlineKeyboardButton("🔍 Review one by one", callback_data=CB_REVIEW)],
            [InlineKeyboardButton("🗑 Discard", callback_data=CB_DISCARD)],
        ]
    )


def step_keyboard(index: int):
    """Keep / drop / re-topic this atom, or go back to the overview."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Keep", callback_data=f"{CB_KEEP}{index}"),
                InlineKeyboardButton("🗑 Drop", callback_data=f"{CB_DROP}{index}"),
                InlineKeyboardButton("🏷 Topic", callback_data=f"{CB_PICK}{index}"),
                InlineKeyboardButton("↩️ Back", callback_data=CB_OVERVIEW),
            ]
        ]
    )


def picker_token_bytes(index: int) -> int:
    """What Telegram's callback limit leaves for a topic token after "mb:ts:<i>:"."""
    return MAX_CALLBACK_BYTES - len(f"{CB_PICK_TOPIC}{index}:".encode())


def topic_picker_keyboard(index: int, topics: list[str], page: int = 0):
    """One button per known topic on this page, prev/next, a new topic, and back."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    page = _clamp_page(topics, page)
    budget = picker_token_bytes(index)
    rows = [
        [
            InlineKeyboardButton(
                topic, callback_data=f"{CB_PICK_TOPIC}{index}:{topic_token(topic, budget)}"
            )
        ]
        for topic in page_slice(topics, page)
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"{CB_PICK_PAGE}{index}:{page - 1}"))
    if page < page_count(topics) - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"{CB_PICK_PAGE}{index}:{page + 1}"))
    if nav:
        rows.append(nav)

    # The atom being picked for is always the first pending one, which is what
    # starting the review shows.
    rows.append(
        [
            InlineKeyboardButton("✍️ New topic", callback_data=f"{CB_PICK_NEW}{index}"),
            InlineKeyboardButton("↩️ Back", callback_data=CB_REVIEW),
        ]
    )
    return InlineKeyboardMarkup(rows)


__all__ = [
    "CB_APPROVE",
    "CB_DISCARD",
    "CB_DROP",
    "CB_KEEP",
    "CB_LIST",
    "CB_OVERVIEW",
    "CB_PAGE",
    "CB_PICK",
    "CB_PICK_NEW",
    "CB_PICK_PAGE",
    "CB_PICK_TOPIC",
    "CB_REVIEW",
    "CB_TOPIC",
    "MAX_CALLBACK_BYTES",
    "TOPIC_TOKEN_BYTES",
    "back_keyboard",
    "picker_token_bytes",
    "resolve_token",
    "review_keyboard",
    "step_keyboard",
    "topic_picker_keyboard",
    "topic_token",
    "topics_keyboard",
]

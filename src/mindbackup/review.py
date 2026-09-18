"""Telegram review UI for extracted atoms.

Rendering and keyboards only. What an approval *means* — which atoms get
filed, what a resolved ambiguity does to an atom — lives in
`proposal.Proposal`, so the bot and the CLI cannot drift apart on it.

The design rule that matters: **the memo is already saved before any of this
runs**. Extraction is a follow-up message, never a gate. If the user ignores
the buttons, walks away, or the model is down, the transcript is still in the
vault — the M1 guarantee is untouched (spec C4).

Split from bot.py so the ingest handler stays readable and this can be tested
without a Telegram server.
"""

from __future__ import annotations

import logging

from .extract import Extraction

logger = logging.getLogger(__name__)

MAX_ATOMS_SHOWN = 12
CB_APPROVE = "mb:ok"
CB_EDIT = "mb:edit"
CB_DISCARD = "mb:no"
CB_RESOLVE = "mb:res"


def _escape(text: str) -> str:
    """Telegram MarkdownV2-lite: we only use *bold*, so neutralise the rest."""
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def render_review(extraction: Extraction, memo_name: str) -> str:
    """The message body the user actually reads. Terse: they're on a phone."""
    atoms = extraction.atoms
    if not atoms:
        return f"🧠 Nothing worth extracting from *{_escape(memo_name)}*."

    lines = [f"🧠 From *{_escape(memo_name)}*:", ""]

    for index, atom in enumerate(atoms[:MAX_ATOMS_SHOWN], start=1):
        mark = "⚠️" if atom.needs_clarification else "•"
        topics = " ".join(f"#{t.replace(' ', '-')}" for t in atom.topics) or "_no topic_"
        lines.append(f"{mark} {index}. {_escape(atom.text)}")
        lines.append(f"     {_escape(topics)}")
        if atom.ambiguity:
            lines.append(f"     ❓ {_escape(atom.ambiguity)}")

    if len(atoms) > MAX_ATOMS_SHOWN:
        lines.append(f"…and {len(atoms) - MAX_ATOMS_SHOWN} more.")

    held = len(extraction.ambiguous)
    if held:
        lines.append("")
        lines.append(f"⚠️ {held} unclear — reply to tell me what you meant.")

    return "\n".join(lines)


def review_keyboard(extraction: Extraction):
    """Approve / edit / discard. Imported lazily so tests need no telegram."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    filed = len(extraction.atoms) - len(extraction.ambiguous)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"✅ File {filed}", callback_data=CB_APPROVE),
                InlineKeyboardButton("✏️ Edit", callback_data=CB_EDIT),
                InlineKeyboardButton("🗑 Discard", callback_data=CB_DISCARD),
            ]
        ]
    )


def render_filed(filed: list) -> str:
    if not filed:
        return "Nothing filed."
    pages = sorted({t for stored in filed for t in stored.topics})
    summary = f"✅ Filed {len(filed)} item(s)"
    if pages:
        summary += " to " + ", ".join(f"*{_escape(p)}*" for p in pages[:6])
        if len(pages) > 6:
            summary += f" +{len(pages) - 6} more"
    return summary


__all__ = [
    "CB_APPROVE",
    "CB_DISCARD",
    "CB_EDIT",
    "CB_RESOLVE",
    "render_filed",
    "render_review",
    "review_keyboard",
]

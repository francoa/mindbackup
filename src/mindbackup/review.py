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

from .proposal import Proposal, Verdict

logger = logging.getLogger(__name__)

MAX_ATOMS_SHOWN = 12
CB_APPROVE = "mb:ok"
CB_DISCARD = "mb:no"
CB_REVIEW = "mb:w"


def _escape(text: str) -> str:
    """Telegram MarkdownV2-lite: we only use *bold*, so neutralise the rest."""
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def _hashtags(topics) -> str:
    return " ".join(f"#{t.replace(' ', '-')}" for t in topics) or "_no topic_"


def render_review(proposal: Proposal) -> str:
    """The overview the user actually reads. Terse: they're on a phone.

    Each atom is marked by where it stands: • pending, ⚠️ pending and unclear,
    ✅ approved, 🗑 rejected, 🏷 reassigned (with the topics the user chose).
    """
    atoms = proposal.atoms
    if not atoms:
        return f"🧠 Nothing worth extracting from *{_escape(proposal.memo_name)}*."

    lines = [f"🧠 From *{_escape(proposal.memo_name)}*:", ""]

    for index, atom in enumerate(atoms[:MAX_ATOMS_SHOWN]):
        decision = proposal.decisions.get(index)
        topics = atom.topics
        if decision is None:
            mark = "⚠️" if atom.needs_clarification else "•"
        elif decision.verdict is Verdict.APPROVE:
            mark = "✅"
        elif decision.verdict is Verdict.REJECT:
            mark = "🗑"
        else:
            mark = "🏷"
            topics = decision.topics
        lines.append(f"{mark} {index + 1}. {_escape(atom.text)}")
        lines.append(f"     {_escape(_hashtags(topics))}")
        if decision is None and atom.ambiguity:
            lines.append(f"     ❓ {_escape(atom.ambiguity)}")

    if len(atoms) > MAX_ATOMS_SHOWN:
        lines.append(f"…and {len(atoms) - MAX_ATOMS_SHOWN} more.")

    unclear = len(proposal.needs_clarification())
    if unclear:
        lines.append("")
        lines.append(f"⚠️ {unclear} unclear — tap 🔍 to sort them out.")

    return "\n".join(lines)


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
    "CB_REVIEW",
    "render_filed",
    "render_review",
    "review_keyboard",
]

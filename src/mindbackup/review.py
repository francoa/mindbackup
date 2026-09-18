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

from .browse import MAX_CALLBACK_BYTES, page_count, page_slice, topic_token
from .proposal import Proposal, Verdict

logger = logging.getLogger(__name__)

MAX_ATOMS_SHOWN = 12
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


def render_atom_step(proposal: Proposal, index: int) -> str:
    """One atom, reviewed on its own: text, topics, the model's question."""
    atom = proposal.atoms[index]
    mark = "⚠️" if atom.needs_clarification else "•"
    lines = [
        f"🔍 *{_escape(proposal.memo_name)}* — {index + 1} of {len(proposal.atoms)}",
        "",
        f"{mark} {_escape(atom.text)}",
        f"     {_escape(_hashtags(atom.topics))}",
    ]
    if atom.ambiguity:
        lines.append(f"     ❓ {_escape(atom.ambiguity)}")
    return "\n".join(lines)


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


def _clamp(topics: list[str], page: int) -> int:
    return max(0, min(page, page_count(topics) - 1))


def render_topic_picker(proposal: Proposal, index: int, topics: list[str], page: int = 0) -> str:
    """The atom being re-topiced, with a line asking for its topic."""
    lines = [render_atom_step(proposal, index), ""]
    if not topics:
        lines.append("🏷 No topics in the vault yet. Tap ✍️ to add one.")
        return "\n".join(lines)
    line = "🏷 Pick a topic:"
    if page_count(topics) > 1:
        line += f" (page {_clamp(topics, page) + 1} of {page_count(topics)})"
    lines.append(line)
    return "\n".join(lines)


def topic_picker_keyboard(index: int, topics: list[str], page: int = 0):
    """One button per known topic on this page, prev/next, a new topic, and back."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    page = _clamp(topics, page)
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


def render_topic_prompt(index: int, retry: bool = False) -> str:
    """The question a ✍️ tap sends; the reply is what `handle_topic_reply` reads."""
    ask = f"✍️ Topics for #{index + 1}, comma-separated:"
    return f"That had no topics in it. {ask}" if retry else ask


def render_reassigned(index: int, topics) -> str:
    """Confirms a typed reply, since the review message it changed may be off screen."""
    return f"🏷 #{index + 1} → {_escape(_hashtags(topics))}"


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
    "CB_DROP",
    "CB_KEEP",
    "CB_OVERVIEW",
    "CB_PICK",
    "CB_PICK_NEW",
    "CB_PICK_PAGE",
    "CB_PICK_TOPIC",
    "CB_REVIEW",
    "picker_token_bytes",
    "render_atom_step",
    "render_filed",
    "render_reassigned",
    "render_review",
    "render_topic_picker",
    "render_topic_prompt",
    "review_keyboard",
    "step_keyboard",
    "topic_picker_keyboard",
]

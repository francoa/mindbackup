"""Telegram message text: topic lists and the review of extracted atoms.

Rendering only. What an approval *means* — which atoms get filed, what a
resolved ambiguity does to an atom — lives in `proposal.Proposal`, so the bot
and the CLI cannot drift apart on it.

The design rule that matters: **the memo is already saved before any of this
runs**. Extraction is a follow-up message, never a gate. If the user ignores
the buttons, walks away, or the model is down, the transcript is still in the
vault — the M1 guarantee is untouched (spec C4).
"""

from __future__ import annotations

from ..proposal import Proposal, Verdict
from ..topic_view import _clamp_page, page_count

MAX_ATOMS_SHOWN = 12


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


def render_topic_picker(proposal: Proposal, index: int, topics: list[str], page: int = 0) -> str:
    """The atom being re-topiced, with a line asking for its topic."""
    lines = [render_atom_step(proposal, index), ""]
    if not topics:
        lines.append("🏷 No topics in the vault yet. Tap ✍️ to add one.")
        return "\n".join(lines)
    line = "🏷 Pick a topic:"
    if page_count(topics) > 1:
        line += f" (page {_clamp_page(topics, page) + 1} of {page_count(topics)})"
    lines.append(line)
    return "\n".join(lines)


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
    "MAX_ATOMS_SHOWN",
    "render_atom_step",
    "render_filed",
    "render_reassigned",
    "render_review",
    "render_topic_list",
    "render_topic_picker",
    "render_topic_prompt",
]

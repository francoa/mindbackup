"""The read side of a topic — what every frontend needs to show one.

Moved out of the Telegram browse UI so a terminal can have it too without
importing keyboards. Nothing here knows about a transport.

What the user sees is the topic *page*, not a re-render of the atom index.
The page is the human-facing artefact and it is the thing they may have
hand-edited in Obsidian; showing anything else would quietly hide their edits.
The index is only a fallback for a topic whose page is missing.
"""

from __future__ import annotations

import logging
import re

from .config import Settings
from .extract import normalise_topic
from .topics import search, topic_page_path, topic_slug

logger = logging.getLogger(__name__)

TOPICS_PER_PAGE = 8
#: Cap on the fallback index render, so a huge topic cannot spam the chat.
MAX_FALLBACK_ATOMS = 200

_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_BLOCK_REF_RE = re.compile(r"[ \t]*\^mb-[0-9a-f]{10}[ \t]*$", re.MULTILINE)


def resolve_topic(name: str, topics: list[str]) -> str | None:
    """A name the user typed (or a slug) back to one of the known topics."""
    name = (name or "").strip()
    if not name:
        return None
    wanted = normalise_topic(name)
    for topic in topics:
        if normalise_topic(topic) == wanted or topic_slug(topic) == topic_slug(name):
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


__all__ = [
    "MAX_FALLBACK_ATOMS",
    "TOPICS_PER_PAGE",
    "page_count",
    "page_slice",
    "resolve_topic",
    "topic_body",
]

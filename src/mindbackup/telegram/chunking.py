"""Splitting text into Telegram-sized messages."""

from __future__ import annotations

#: Telegram rejects a message over 4096 characters; leave room for the header.
MAX_MESSAGE_CHARS = 3500


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


__all__ = ["MAX_MESSAGE_CHARS", "chunk_message"]

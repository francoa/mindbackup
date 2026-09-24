"""Terminal presentation for the CLI: status marks, stdout/stderr writers, atom rendering."""

from __future__ import annotations

import sys

OK = "✓"
BAD = "✗"
WARN = "!"


def out(message: str = "") -> None:
    """User-facing output: the answer they ran the command for."""
    print(message, file=sys.stdout)


def err(message: str) -> None:
    """User-facing error, on stderr so it survives stdout redirection."""
    print(message, file=sys.stderr)


def render_atom(atom, prefix: str = "   ") -> str:
    topics = " ".join(f"[[{t}]]" for t in atom.topics) or "(no topic)"
    flag = "  ⚠" if atom.needs_clarification else ""
    line = f"{prefix}{atom.kind:8} {atom.text}\n{prefix}         {topics}{flag}"
    if atom.ambiguity:
        line += f"\n{prefix}         ? {atom.ambiguity}"
    return line

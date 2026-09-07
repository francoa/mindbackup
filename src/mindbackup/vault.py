"""Turn a transcript into a markdown file in the vault.

Milestone 1 rules (spec §5), deliberately not exceeded:
  - `<vault>/Memos/<YYYY-MM-DD> <short-title>.md`
  - frontmatter: date, type: memo, source: telegram
  - body: the full raw transcript, nothing else

C4 says this layer is immutable and complete: never overwrite an existing
memo, and never lose a transcript to a filename collision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

TITLE_WORD_COUNT = 6
TITLE_MAX_CHARS = 60
FALLBACK_TITLE = "memo"


class VaultWriteError(Exception):
    """The memo could not be written. Surfaced to the user in Telegram."""


@dataclass(frozen=True)
class Memo:
    path: Path
    title: str
    memo_date: date


def render_memo(transcript: str, memo_date: date, source: str = "telegram") -> str:
    """Render the markdown document. Minimal frontmatter, then raw transcript."""
    body = transcript.strip()
    return (
        "---\n"
        f"date: {memo_date.isoformat()}\n"
        "type: memo\n"
        f"source: {source}\n"
        "---\n"
        "\n"
        f"{body}\n"
    )


def _unique_path(directory: Path, stem: str) -> Path:
    """`stem.md`, or `stem 2.md`, `stem 3.md`... Never returns an existing path."""
    candidate = directory / f"{stem}.md"
    if not candidate.exists():
        return candidate
    for suffix in range(2, 1000):
        candidate = directory / f"{stem}_{suffix}.md"
        if not candidate.exists():
            return candidate
    raise VaultWriteError(f"Could not find a free filename for {stem!r} in {directory}.")


def write_memo(
    transcript: str,
    memo_date: date,
    memo_dir: Path,
    source: str = "telegram",
) -> Memo:
    """Write the transcript into the vault. Returns the created Memo."""
    text = (transcript or "").strip()
    if not text:
        raise VaultWriteError("Refusing to write an empty transcript to the vault.")

    try:
        memo_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VaultWriteError(f"Cannot create memo folder {memo_dir}: {exc}") from exc

    path = _unique_path(memo_dir, memo_date.isoformat())

    try:
        # x mode: fail loudly rather than clobber an existing memo (C4).
        with path.open("x", encoding="utf-8") as handle:
            handle.write(render_memo(text, memo_date, source))
    except FileExistsError as exc:  # lost a race with a concurrent write
        raise VaultWriteError(f"{path.name} appeared while writing it.") from exc
    except OSError as exc:
        raise VaultWriteError(f"Cannot write {path}: {exc}") from exc

    return Memo(path=path, title=path.stem, memo_date=memo_date)

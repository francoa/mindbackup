"""Topic pages: the consolidation layer (spec §6, M2).

The one thing grep genuinely cannot do — gather what you said about a subject
across twelve separate ramblings onto one page.

Two stores, both derived and both re-buildable from raw transcripts (C4):

  - `<vault>/Topics/<slug>.md` — human-facing, Obsidian-native, one page per
    topic, flat. Cross-cutting is handled by links, not nesting (C7).
  - `<vault>/.mindbackup/atoms.jsonl` — machine-facing index that `ask` queries.

Appends are idempotent. Every bullet ends in an Obsidian block reference
`^mb-<id>` derived from (memo, text), so re-running extraction over a memo
updates nothing twice, and your hand-edits to a topic page survive — we never
rewrite a page wholesale.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

from .config import Settings
from .extract import Atom, normalise_topic
from .vault import VaultWriteError

logger = logging.getLogger(__name__)

ATOM_INDEX_NAME = "atoms.jsonl"
BLOCK_REF_RE = re.compile(r"\^mb-([0-9a-f]{10})\s*$", re.MULTILINE)
DEFAULT_TOPIC = "cambalache"


@dataclass(frozen=True)
class StoredAtom:
    """An atom as filed: the atom plus where it came from."""

    id: str
    text: str
    kind: str
    topics: list[str]
    memo: str
    memo_date: str
    audio: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "kind": self.kind,
            "topics": self.topics,
            "memo": self.memo,
            "memo_date": self.memo_date,
            "audio": self.audio,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StoredAtom":
        return cls(
            id=str(data.get("id", "")),
            text=str(data.get("text", "")),
            kind=str(data.get("kind", "fact")),
            topics=list(data.get("topics") or []),
            memo=str(data.get("memo", "")),
            memo_date=str(data.get("memo_date", "")),
            audio=data.get("audio") or None,
        )


def atom_id(memo_name: str, text: str) -> str:
    """Stable short id for (memo, atom text). Survives re-extraction."""
    digest = hashlib.sha256(f"{memo_name}\x00{text.strip()}".encode("utf-8"))
    return digest.hexdigest()[:10]


def topic_slug(topic: str) -> str:
    """Topic name -> safe flat filename stem."""
    slug = re.sub(r"[^\w\s-]", "", normalise_topic(topic), flags=re.UNICODE)
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug or "untitled"


def _existing_block_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        return set(BLOCK_REF_RE.findall(path.read_text(encoding="utf-8")))
    except OSError as exc:
        logger.warning("Could not read topic page %s: %s", path, exc)
        return set()


def _render_bullet(stored: StoredAtom) -> str:
    """One topic-page bullet: the sentence, its date, and the way back."""
    kind = f"**{stored.kind}** " if stored.kind != "fact" else ""
    memo_link = f"[[{stored.memo}]]"
    audio = f" · `{stored.audio}`" if stored.audio else ""
    return f"- {kind}{stored.text} — {stored.memo_date} {memo_link}{audio} ^mb-{stored.id}"


def _new_topic_page(topic: str) -> str:
    return (
        "---\n"
        "type: topic\n"
        f"topic: {topic}\n"
        "---\n"
        f"\n# {topic}\n\n"
    )


def append_to_topic(topic: str, stored: StoredAtom, settings: Settings) -> Path:
    """Append one atom to its topic page, creating the page if needed."""
    topic_dir = settings.topic_path
    try:
        topic_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VaultWriteError(f"Cannot create topic folder {topic_dir}: {exc}") from exc

    path = topic_dir / f"{topic_slug(topic)}.md"

    if stored.id in _existing_block_ids(path):
        logger.debug("Atom %s already on %s, skipping.", stored.id, path.name)
        return path

    try:
        if not path.exists():
            path.write_text(_new_topic_page(topic), encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_render_bullet(stored) + "\n")
    except OSError as exc:
        raise VaultWriteError(f"Cannot write topic page {path}: {exc}") from exc

    return path


def index_path(settings: Settings) -> Path:
    return settings.state_path / ATOM_INDEX_NAME


def append_to_index(
    stored: StoredAtom, settings: Settings, known_ids: set[str] | None = None
) -> None:
    """Record the atom in the queryable index, skipping duplicates.

    `known_ids` lets a batch caller avoid rescanning the index per atom; it is
    updated in place so successive appends in the same batch stay deduped.
    """
    path = index_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VaultWriteError(f"Cannot create state folder {path.parent}: {exc}") from exc

    if known_ids is None:
        known_ids = {existing.id for existing in iter_index(settings)}
    if stored.id in known_ids:
        return
    known_ids.add(stored.id)

    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stored.to_dict(), ensure_ascii=False) + "\n")
    except OSError as exc:
        raise VaultWriteError(f"Cannot write atom index {path}: {exc}") from exc


def iter_index(settings: Settings) -> Iterator[StoredAtom]:
    """Every atom filed so far. Tolerates a partially-written last line."""
    path = index_path(settings)
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read atom index %s: %s", path, exc)
        return
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield StoredAtom.from_dict(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed line in atom index.")


def known_topics(settings: Settings) -> list[str]:
    """Topics the vault already knows, most-used first.

    Fed to the extractor so it reuses `voice-mind-backup` instead of coining
    `the voice project` — the whole point of the resolution step.

    Topic pages are matched back to their canonical name by slug, not by
    de-slugifying the filename: `voice-mind-backup.md` must not re-enter the
    list as the near-duplicate "voice mind backup" and undo the deduping.
    """
    counts: dict[str, int] = {}
    for stored in iter_index(settings):
        for topic in stored.topics:
            counts[topic] = counts.get(topic, 0) + 1

    topic_dir = settings.topic_path
    if topic_dir.is_dir():
        known_slugs = {topic_slug(t) for t in counts}
        for page in topic_dir.glob("*.md"):
            if page.stem in known_slugs:
                continue  # already represented under its canonical name
            counts.setdefault(_topic_name_from_page(page), 0)

    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _topic_name_from_page(page: Path) -> str:
    """Recover a topic's name from its page, preferring the frontmatter."""
    try:
        head = page.read_text(encoding="utf-8")[:400]
    except OSError:
        head = ""
    match = re.search(r"^topic:\s*(.+)$", head, re.MULTILINE)
    if match:
        name = normalise_topic(match.group(1))
        if name:
            return name
    return page.stem.replace("-", " ")


def file_atoms(
    atoms: Iterable[Atom],
    memo_name: str,
    memo_date: date | str,
    settings: Settings,
    audio: str | None = None,
) -> list[StoredAtom]:
    """File approved atoms into the index and their topic pages.

    Atoms with no topic still land in the index — they stay searchable via
    `ask` even when nothing consolidates them.
    """
    date_str = memo_date.isoformat() if isinstance(memo_date, date) else str(memo_date)
    filed: list[StoredAtom] = []
    known_ids = {existing.id for existing in iter_index(settings)}

    for atom in atoms:
        text = atom.get_text().strip()
        if not text:
            continue
        stored = StoredAtom(
            id=atom_id(memo_name, text),
            text=text,
            kind=atom.kind,
            topics=list(atom.topics),
            memo=memo_name,
            memo_date=date_str,
            audio=audio,
        )
        append_to_index(stored, settings, known_ids)
        for topic in stored.topics:
            append_to_topic(topic, stored, settings)
        if not stored.topics:
            # Assign a default topic so that it appears in Obsidian
            append_to_topic(DEFAULT_TOPIC, stored, settings)
        filed.append(stored)

    logger.info("Filed %d atom(s) from %s.", len(filed), memo_name)
    return filed


def search(
    settings: Settings,
    query: str = "",
    *,
    kind: str | None = None,
    topic: str | None = None,
    limit: int = 50,
) -> list[StoredAtom]:
    """Substring + facet search over filed atoms, newest first.

    Not semantic — but it searches *resolved* sentences, so "voice mind backup"
    now matches an atom you originally spoke as "this project".
    """
    terms = [t for t in query.lower().split() if t]
    wanted_topic = normalise_topic(topic) if topic else None

    results = []
    for stored in iter_index(settings):
        if kind and stored.kind != kind:
            continue
        if wanted_topic and wanted_topic not in [normalise_topic(t) for t in stored.topics]:
            continue
        haystack = f"{stored.text} {' '.join(stored.topics)} {stored.memo}".lower()
        if terms and not all(term in haystack for term in terms):
            continue
        results.append(stored)

    results.sort(key=lambda s: (s.memo_date, s.memo), reverse=True)
    return results[:limit]


__all__ = [
    "StoredAtom",
    "append_to_index",
    "append_to_topic",
    "atom_id",
    "file_atoms",
    "iter_index",
    "known_topics",
    "search",
    "topic_slug",
]

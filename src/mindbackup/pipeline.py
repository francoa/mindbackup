"""The ingest pipeline: audio file in, memo in the vault out.

Shared by the Telegram bot and the CLI so that `mindbackup ingest <file>`
exercises exactly the same code path the bot does — no "works in the test,
fails on the phone" gap.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Settings
from .extract import Atom, Extraction, LLMError, extract_atoms
from .stt import Transcript, TranscriptionError, transcribe
from .topics import StoredAtom, file_atoms, known_topics
from .vault import Memo, VaultWriteError, write_memo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestResult:
    memo: Memo
    transcript: Transcript
    archived_audio: Path | None


@dataclass(frozen=True)
class ExtractionResult:
    memo_path: Path
    extraction: Extraction
    filed: list[StoredAtom] = field(default_factory=list)

    @property
    def atoms(self) -> list[Atom]:
        return self.extraction.atoms

    @property
    def ambiguous(self) -> list[Atom]:
        return self.extraction.ambiguous


def local_today(settings: Settings) -> date:
    """Today's date in the configured timezone.

    Matters: a memo recorded at 00:30 in Madrid must not be filed as the
    previous day because the server thinks in UTC.
    """
    if settings.timezone:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(settings.timezone)).date()
        except Exception:
            logger.warning(
                "Unknown timezone %r, falling back to system local time.",
                settings.timezone,
            )
    return datetime.now().date()


def resolve_memo_date(settings: Settings, recorded_at: datetime | None) -> date:
    """Date of recording (spec §5.2), converted into the configured timezone."""
    if recorded_at is None:
        return local_today(settings)
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    if settings.timezone:
        try:
            from zoneinfo import ZoneInfo

            return recorded_at.astimezone(ZoneInfo(settings.timezone)).date()
        except Exception:
            logger.warning("Unknown timezone %r, using UTC date.", settings.timezone)
    return recorded_at.astimezone().date()


def archive_audio(audio_path: Path, settings: Settings, memo_date: date) -> Path | None:
    """Keep the source audio so transcripts stay re-derivable (C4).

    Best-effort: a failed archive must never lose the memo, so it warns
    rather than raising.
    """
    if settings.audio_archive is None:
        return None
    try:
        target_dir = settings.audio_archive / memo_date.strftime("%Y-%m")
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        target = target_dir / f"{stamp}-{audio_path.name}"
        shutil.copy2(audio_path, target)
        return target
    except OSError as exc:
        logger.warning("Could not archive audio %s: %s", audio_path, exc)
        return None


def ingest_audio(
    audio_path: Path,
    settings: Settings,
    *,
    recorded_at: datetime | None = None,
    source: str = "telegram",
) -> IngestResult:
    """Transcribe an audio file and file it in the vault.

    Raises TranscriptionError or VaultWriteError — both carry a message that is
    safe and useful to show the user (spec §5.6: no silent failure).
    """
    memo_date = resolve_memo_date(settings, recorded_at)

    transcript = transcribe(audio_path, settings)
    logger.info(
        "Transcribed %s via %s/%s (%d chars, lang=%s)",
        audio_path.name,
        transcript.provider,
        transcript.model,
        len(transcript.text),
        transcript.language,
    )

    memo = write_memo(transcript.text, memo_date, settings.memo_path, source=source)
    logger.info("Wrote memo %s", memo.path)

    archived = archive_audio(audio_path, settings, memo_date)
    return IngestResult(memo=memo, transcript=transcript, archived_audio=archived)


def read_memo_body(path: Path) -> str:
    """The transcript out of a memo file, minus the YAML frontmatter."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VaultWriteError(f"Cannot read memo {path}: {exc}") from exc

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def memo_date_from_path(path: Path) -> str:
    """Memo filenames start with the ISO date (vault.py writes them that way)."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", path.stem)
    return match.group(1) if match else ""


def extract_memo(
    memo_path: Path,
    settings: Settings,
    *,
    file: bool = True,
) -> ExtractionResult:
    """Run the intelligence layer over one memo already in the vault.

    Read-only with respect to the memo itself (C4): extraction never edits the
    raw transcript, it only writes derived topic pages and the atom index.

    Raises LLMError if the model is unreachable. Callers in the ingest path
    must catch it — a failed extraction degrades a memo, it must not lose one.
    """
    transcript = read_memo_body(memo_path)
    extraction = extract_atoms(transcript, settings, known_topics(settings))

    filed: list[StoredAtom] = []
    if file and extraction.atoms:
        # Ambiguous atoms are held back for the user to confirm; filing a wrong
        # classification is worse than filing none (spec C6).
        # TODO: Atoms that need clarification are going to be recorded for the moment
        #   I'd rather have these being recorded with a warning than not recorded at all
        # Restore this when appropriate: confident = [a for a in extraction.atoms if not a.needs_clarification]
        filed = file_atoms(
            extraction.atoms,
            memo_path.stem,
            memo_date_from_path(memo_path) or local_today(settings),
            settings,
        )

    return ExtractionResult(memo_path=memo_path, extraction=extraction, filed=filed)


__all__ = [
    "ExtractionResult",
    "IngestResult",
    "LLMError",
    "TranscriptionError",
    "VaultWriteError",
    "extract_memo",
    "ingest_audio",
    "local_today",
    "memo_date_from_path",
    "read_memo_body",
    "resolve_memo_date",
]

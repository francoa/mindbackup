"""CLI: `mindbackup doctor | ingest | bot`.

Output split, deliberately:
  - `out()` / `err()` write the human-facing report to stdout/stderr, so
    `mindbackup ingest ... > file` captures something and `doctor` stays
    readable.
  - `logging` carries diagnostics (and everything the bot emits), so the
    container/systemd log has timestamps and levels.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from mindbackup.config import ConfigError, Settings, load_settings
from mindbackup.extract import KINDS
from mindbackup.pipeline import TranscriptionError, VaultWriteError, ingest_audio

logger = logging.getLogger(__name__)

OK = "✓"
BAD = "✗"
WARN = "!"


def out(message: str = "") -> None:
    """User-facing output: the answer they ran the command for."""
    print(message, file=sys.stdout)


def err(message: str) -> None:
    """User-facing error, on stderr so it survives stdout redirection."""
    print(message, file=sys.stderr)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    """Check every precondition the ingest path depends on. No token required."""
    problems = 0

    def check(ok: bool, label: str, detail: str = "", fatal: bool = True) -> None:
        nonlocal problems
        mark = OK if ok else (BAD if fatal else WARN)
        out(f" {mark} {label}" + (f" — {detail}" if detail else ""))
        if not ok and fatal:
            problems += 1

    out("\nVault")
    vault_exists = settings.vault_path.is_dir()
    check(
        vault_exists,
        f"vault exists: {settings.vault_path}",
        "" if vault_exists else "will be created on first memo",
        fatal=False,
    )
    memo_dir = settings.memo_path
    writable = False
    try:
        memo_dir.mkdir(parents=True, exist_ok=True)
        probe = memo_dir / ".mindbackup-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError as exc:
        check(False, f"memo folder is writable: {memo_dir}", str(exc))
    if writable:
        check(True, f"memo folder is writable: {memo_dir}")

    out("\nTranscription")
    out(f"   provider: {settings.stt_provider}  model: {settings.stt_model}  "
        f"language: {settings.stt_language or 'auto-detect'}")
    if settings.vocabulary:
        out(f"   vocabulary hint: {settings.vocabulary[:70]}")
    else:
        check(False, "vocabulary hint set", "MINDBACKUP_VOCABULARY improves jargon accuracy", fatal=False)
    try:
        import faster_whisper  # noqa: F401

        check(True, "faster-whisper installed")
    except ImportError:
        check(False, "faster-whisper installed", "pip install 'voice-mind-backup[local]'")
    check(shutil.which("ffmpeg") is not None, "ffmpeg on PATH", fatal=False)

    out("\nTelegram")
    try:
        import telegram  # noqa: F401

        check(True, "python-telegram-bot installed")
    except ImportError:
        check(False, "python-telegram-bot installed", "pip install voice-mind-backup")
    check(bool(settings.telegram_token), "bot token set", "MINDBACKUP_TELEGRAM_TOKEN", fatal=False)
    check(
        bool(settings.allowed_users),
        f"allowlist set ({len(settings.allowed_users)} user(s))",
        "MINDBACKUP_ALLOWED_USERS",
        fatal=False,
    )

    out("\nAudio archive")
    if settings.audio_archive is None:
        check(True, "disabled (raw audio not kept)", fatal=False)
    else:
        try:
            settings.audio_archive.mkdir(parents=True, exist_ok=True)
            check(True, f"writable: {settings.audio_archive}")
        except OSError as exc:
            check(False, f"writable: {settings.audio_archive}", str(exc), fatal=False)

    out("\nIntelligence layer (extract / ask)")
    if not settings.llm_configured:
        check(
            False,
            "LLM configured",
            "set MINDBACKUP_LLM_MODEL and MINDBACKUP_LLM_API_KEY to enable extraction",
            fatal=False,
        )
    else:
        check(True, f"LLM: {settings.llm_model} via {settings.llm_base_url}")
        try:
            topic_dir = settings.topic_path
            topic_dir.mkdir(parents=True, exist_ok=True)
            check(True, f"topic folder is writable: {topic_dir}")
        except OSError as exc:
            check(False, f"topic folder is writable: {settings.topic_path}", str(exc))
        try:
            from mindbackup.topics import iter_index

            filed = sum(1 for _ in iter_index(settings))
            check(True, f"atom index: {filed} atom(s) filed", fatal=False)
        except Exception as exc:
            check(False, "atom index readable", str(exc), fatal=False)

    if problems:
        err(f"\n{BAD} {problems} blocking problem(s).\n")
        return 1
    out(f"\n{OK} Ready to ingest.\n")
    return 0


def cmd_ingest(args: argparse.Namespace, settings: Settings) -> int:
    """Transcribe a local audio file through the exact pipeline the bot uses."""
    audio_path = Path(args.audio).expanduser()
    if not audio_path.is_file():
        err(f"{BAD} No such file: {audio_path}")
        return 1

    recorded_at = None
    if args.date:
        try:
            recorded_at = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
        except ValueError:
            err(f"{BAD} --date must be ISO format (YYYY-MM-DD).")
            return 1

    try:
        result = ingest_audio(
            audio_path, settings, recorded_at=recorded_at, source=args.source
        )
    except (TranscriptionError, VaultWriteError) as exc:
        logger.debug("Ingest failed", exc_info=True)
        err(f"{BAD} {exc}")
        return 1

    out(f"{OK} {result.memo.path}")
    out(f"   {len(result.transcript.text)} chars, language={result.transcript.language}")
    if result.archived_audio:
        out(f"   audio archived: {result.archived_audio}")
    return 0


def cmd_bot(args: argparse.Namespace, settings: Settings) -> int:
    from mindbackup.bot import run

    run(settings)
    return 0


def _memo_targets(args: argparse.Namespace, settings: Settings) -> list[Path]:
    """Which memos to extract: an explicit path, or the whole Memos folder."""
    if args.memo:
        path = Path(args.memo).expanduser()
        if not path.is_absolute() and not path.exists():
            path = settings.memo_path / args.memo
        if not path.is_file():
            raise FileNotFoundError(path)
        return [path]
    if not settings.memo_path.is_dir():
        return []
    return sorted(settings.memo_path.glob("*.md"))


def _render_atom(atom, prefix: str = "   ") -> str:
    topics = " ".join(f"[[{t}]]" for t in atom.topics) or "(no topic)"
    flag = "  ⚠" if atom.needs_clarification else ""
    line = f"{prefix}{atom.kind:8} {atom.text}\n{prefix}         {topics}{flag}"
    if atom.ambiguity:
        line += f"\n{prefix}         ? {atom.ambiguity}"
    return line


def cmd_extract(args: argparse.Namespace, settings: Settings) -> int:
    """Run the summariser + classifier over memos already in the vault."""
    from mindbackup.pipeline import LLMError, extract_memo
    from mindbackup.topics import iter_index

    try:
        memos = _memo_targets(args, settings)
    except FileNotFoundError as exc:
        err(f"{BAD} No such memo: {exc}")
        return 1

    if not memos:
        err(f"{WARN} No memos found in {settings.memo_path}.")
        return 1

    if not args.all and not args.memo:
        # Default to memos that have never been extracted, so re-running is
        # cheap and does not re-bill the whole vault.
        seen = {stored.memo for stored in iter_index(settings)}
        memos = [m for m in memos if m.stem not in seen]
        if not memos:
            out(f"{OK} All memos already extracted. Use --all to redo them.")
            return 0

    if args.limit:
        memos = memos[: args.limit]

    total_atoms = total_filed = failures = 0

    for memo_path in memos:
        out(f"\n{memo_path.name}")
        try:
            result = extract_memo(memo_path, settings, file=not args.dry_run)
        except LLMError as exc:
            err(f" {BAD} {exc}")
            failures += 1
            continue
        except VaultWriteError as exc:
            err(f" {BAD} {exc}")
            failures += 1
            continue

        if result.extraction.summary:
            out(f"   … {result.extraction.summary}")
        if not result.atoms:
            out(f"   {WARN} nothing worth extracting")
            continue

        for atom in result.atoms:
            out(_render_atom(atom))
        total_atoms += len(result.atoms)
        total_filed += len(result.filed)

        held = len(result.ambiguous)
        if held:
            out(f"   {WARN} {held} atom(s) held back pending your confirmation")

    verb = "would file" if args.dry_run else "filed"
    out(f"\n{OK} {len(memos)} memo(s): {total_atoms} atom(s), {verb} {total_filed}.")
    if failures:
        err(f"{BAD} {failures} memo(s) failed.")
        return 1
    return 0


def cmd_ask(args: argparse.Namespace, settings: Settings) -> int:
    """Query filed atoms. The retrieval half of the intelligence layer."""
    from mindbackup.topics import iter_index, known_topics, search

    if args.topics:
        topics = known_topics(settings)
        if not topics:
            err(f"{WARN} No topics yet. Run `mindbackup extract` first.")
            return 1
        out(f"\n{len(topics)} topic(s):\n")
        for topic in topics:
            out(f"   {topic}")
        out("")
        return 0

    results = search(
        settings,
        " ".join(args.query),
        kind=args.kind,
        topic=args.topic,
        limit=args.limit,
    )

    if not results:
        total = sum(1 for _ in iter_index(settings))
        if total == 0:
            err(f"{WARN} Nothing extracted yet. Run `mindbackup extract` first.")
        else:
            err(f"{WARN} No match in {total} atom(s). Try `--topics` to see what exists.")
        return 1

    out("")
    for stored in results:
        topics = " ".join(f"[[{t}]]" for t in stored.topics) or "(no topic)"
        out(f" {stored.kind:8} {stored.text}")
        out(f"          {stored.memo_date}  {topics}  → {stored.memo}")
    out(f"\n{OK} {len(results)} result(s).\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mindbackup",
        description="Voice memo -> transcript -> Obsidian vault.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check configuration and dependencies")
    doctor.set_defaults(func=cmd_doctor)

    ingest = sub.add_parser("ingest", help="transcribe a local audio file into the vault")
    ingest.add_argument("audio", help="path to an audio file")
    ingest.add_argument("--date", help="recording date, ISO YYYY-MM-DD (default: today)")
    ingest.add_argument("--source", default="cli", help="frontmatter source value")
    ingest.set_defaults(func=cmd_ingest)

    bot = sub.add_parser("bot", help="run the Telegram ingest bot")
    bot.set_defaults(func=cmd_bot)

    extract = sub.add_parser(
        "extract",
        help="summarise + classify memos into topic pages",
        description=(
            "Reads memos already in the vault, extracts the meaningful "
            "sentences, resolves what they refer to, and files them onto "
            "Topics/ pages. Never modifies the raw transcripts."
        ),
    )
    extract.add_argument("memo", nargs="?", help="one memo file (default: all new ones)")
    extract.add_argument("--all", action="store_true", help="re-extract already-processed memos")
    extract.add_argument("--dry-run", action="store_true", help="show atoms, write nothing")
    extract.add_argument("--limit", type=int, help="stop after N memos")
    extract.set_defaults(func=cmd_extract)

    ask = sub.add_parser(
        "ask",
        help="search extracted atoms",
        description=(
            "Searches the resolved sentences, so a query matches the canonical "
            "name even when you spoke a pronoun."
        ),
    )
    ask.add_argument("query", nargs="*", help="words to match")
    ask.add_argument("--kind", choices=KINDS, help="only this kind of atom")
    ask.add_argument("--topic", help="only atoms on this topic")
    ask.add_argument("--topics", action="store_true", help="list known topics and exit")
    ask.add_argument("--limit", type=int, default=50, help="max results (default 50)")
    ask.set_defaults(func=cmd_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        settings = load_settings()
    except ConfigError as exc:
        err(f"{BAD} Configuration error: {exc}")
        return 2
    try:
        return args.func(args, settings)
    except ConfigError as exc:
        err(f"{BAD} Configuration error: {exc}")
        return 2
    except KeyboardInterrupt:
        err("\nStopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

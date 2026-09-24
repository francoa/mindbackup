import argparse
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from mindbackup.cli.render_utils import BAD, OK, WARN, err, out, render_atom
from mindbackup.config import Settings
from mindbackup.pipeline import TranscriptionError, VaultWriteError, ingest_audio
from mindbackup.proposal import parse_topics
from mindbackup.topics import known_topics

logger = logging.getLogger(__name__)


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
    out(
        f"   provider: {settings.stt_provider}  model: {settings.stt_model}  "
        f"language: {settings.stt_language or 'auto-detect'}"
    )
    if settings.vocabulary:
        out(f"   vocabulary hint: {settings.vocabulary[:70]}")
    else:
        check(
            False,
            "vocabulary hint set",
            "MINDBACKUP_VOCABULARY improves jargon accuracy",
            fatal=False,
        )
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
        result = ingest_audio(audio_path, settings, recorded_at=recorded_at, source=args.source)
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
    from mindbackup.telegram.bot import run

    run(settings)
    return 0


def _memo_targets(args: argparse.Namespace, settings: Settings) -> list[Path]:
    """Which memos to extract: an explicit path, or the whole Memos folder."""
    from mindbackup.vault import iter_memos

    if args.memo:
        path = Path(args.memo).expanduser()
        if not path.is_absolute() and not path.exists():
            path = settings.memo_path / args.memo
        if not path.is_file():
            # Named a memo that has been filed into a subfolder by hand.
            wanted = Path(args.memo).name
            moved = [m for m in iter_memos(settings.memo_path) if m.name == wanted]
            if len(moved) == 1:
                return moved
            raise FileNotFoundError(path)
        return [path]
    return iter_memos(settings.memo_path)


def _ask(prompt: str) -> str:
    """One line from the user. EOF (piped stdin, Ctrl-D) reads as "quit"."""
    try:
        return input(prompt).strip()
    except EOFError:
        out("")
        return "q"


def _ask_topics(known: list[str]) -> list[str]:
    """Which topics this atom really belongs to, answering the model's question."""
    if known:
        shown = ", ".join(known[:20]) + ("…" if len(known) > 20 else "")
        out(f"            known: {shown}")
    return parse_topics(_ask("            topics (comma-separated): "))


def _confirm_interactively(proposal, settings: Settings) -> bool:
    """Walk the pending atoms with the user. False means they quit early.

    Note what is *not* here: no filing, no ambiguity handling, no stage
    ordering. Rendering and prompting only — the same operations the bot's
    buttons call. That is the test of whether `Proposal` is the right object.
    """
    known = known_topics(settings)

    for index, atom in proposal.pending():
        out("")
        out(render_atom(atom))
        while True:
            answer = (_ask("            [a]pprove / [r]eject / [t]opics / [q]uit: ") or "a").lower()
            if answer == "a":
                proposal.approve(index)
            elif answer == "r":
                proposal.reject(index)
            elif answer == "t":
                proposal.reassign(index, _ask_topics(known))
            elif answer == "q":
                return False
            else:
                err("            ? answer a, r, t or q.")
                continue
            break
    return True


def cmd_extract(args: argparse.Namespace, settings: Settings) -> int:
    """Run the summariser + classifier over memos already in the vault."""
    from mindbackup.pipeline import LLMError, propose_from_memo
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

    total_atoms = total_filed = total_held = failures = 0
    reviewed = 0

    for memo_path in memos:
        reviewed += 1
        out(f"\n{memo_path.name}")
        try:
            proposal = propose_from_memo(memo_path, settings)
        except LLMError as exc:
            err(f" {BAD} {exc}")
            failures += 1
            continue
        except VaultWriteError as exc:
            err(f" {BAD} {exc}")
            failures += 1
            continue

        if proposal.summary:
            out(f"   … {proposal.summary}")
        if not proposal.atoms:
            out(f"   {WARN} nothing worth extracting")
            continue

        total_atoms += len(proposal.atoms)
        finished = True

        if args.interactive:
            finished = _confirm_interactively(proposal, settings)
        else:
            for atom in proposal.atoms:
                out(render_atom(atom))
            # No one to ask: the batch policy is to file the lot, unclear atoms
            # included, and say so rather than pretending they were confirmed.
            unclear = len(proposal.needs_clarification())
            proposal.approve_all()
            if unclear:
                out(f"   {WARN} {unclear} atom(s) unclear — filed anyway (see --interactive)")

        if args.dry_run:
            # "Build the proposal, print it, never commit" — the flag enforced
            # by not calling the one method that writes, rather than by a
            # boolean threaded down two call levels.
            total_filed += len(proposal.approved())
        else:
            try:
                total_filed += len(proposal.commit(settings))
            except VaultWriteError as exc:
                err(f" {BAD} {exc}")
                failures += 1
                continue

        total_held += len(proposal.pending())

        if not finished:
            out(f"   {WARN} stopped — {len(proposal.pending())} atom(s) left undecided")
            break

    verb = "would file" if args.dry_run else "filed"
    out(f"\n{OK} {reviewed} memo(s): {total_atoms} atom(s), {verb} {total_filed}.")
    if total_held:
        out(f"{WARN} {total_held} atom(s) left undecided — not filed, not lost.")
    if failures:
        err(f"{BAD} {failures} memo(s) failed.")
        return 1
    return 0


def cmd_delete(args: argparse.Namespace, settings: Settings) -> int:
    """Admin: remove a memo and everything filed from it. CLI-only on purpose."""
    from mindbackup.topics import remove_memo

    if not args.memo.endswith(".md"):
        args.memo += ".md"
    try:
        memo_path: Path | None = _memo_targets(args, settings)[0]
    except FileNotFoundError:
        # The memo may already be gone by hand; its atoms and bullets can
        # still be cleaned up by name.
        memo_path = None
    memo_name = memo_path.stem if memo_path else Path(args.memo).stem

    plan = remove_memo(memo_name, settings, dry_run=True)
    if memo_path is None and not plan.atoms and not plan.bullets:
        err(f"{BAD} No such memo, and nothing filed from it: {memo_name}")
        return 1

    out(f"\n{memo_name}")
    if memo_path:
        out(f"   memo:  {memo_path}")
    else:
        out(f"   {WARN} memo file already gone — cleaning up what was filed from it")
    out(f"   atoms: {len(plan.atoms)} in the index")
    for page, count in plan.bullets.items():
        out(f"   topic: {page.relative_to(settings.topic_path)} ({count} bullet(s))")
    out("")

    if args.dry_run:
        out(f"{OK} Dry run — nothing deleted.")
        return 0
    if not args.yes and _ask("Delete all of this? [y/N] ").lower() not in ("y", "yes"):
        out("Nothing deleted.")
        return 1

    try:
        # Derived data first: if this fails the memo is still there to retry.
        removal = remove_memo(memo_name, settings)
        if memo_path:
            memo_path.unlink()
    except (VaultWriteError, OSError) as exc:
        err(f"{BAD} {exc}")
        return 1

    out(
        f"{OK} Deleted {memo_name}: {len(removal.atoms)} atom(s), "
        f"{sum(removal.bullets.values())} topic bullet(s)."
    )
    return 0

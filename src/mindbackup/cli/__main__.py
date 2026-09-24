"""CLI: `mindbackup doctor | ingest | bot | extract | ask | delete`.

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
import sys

from mindbackup.cli.commands import cmd_bot, cmd_delete, cmd_doctor, cmd_extract, cmd_ingest
from mindbackup.cli.render_utils import BAD, err
from mindbackup.config import ConfigError, load_settings

logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


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
    extract.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="confirm each atom before filing (approve / reject / retopic)",
    )
    extract.add_argument("--limit", type=int, help="stop after N memos")
    extract.set_defaults(func=cmd_extract)

    delete = sub.add_parser(
        "delete",
        help="admin: delete a memo and everything filed from it",
        description=(
            "Deletes the memo, its atoms from the index and its bullets from "
            "the topic pages. Asks first. Archived audio is left alone."
        ),
    )
    delete.add_argument("memo", help="memo file name, with or without .md (e.g. 2026-09-18_3)")
    delete.add_argument("--dry-run", action="store_true", help="show what would go, delete nothing")
    delete.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    delete.set_defaults(func=cmd_delete)

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

"""Video link in, transcript out, via a user-configured command.

Nothing here knows which tool fetches the subtitles. MINDBACKUP_VIDEO_COMMAND
is run with `{url}` and `{out_dir}` filled in, and must write subtitle files
(`.vtt` or `.srt`) into `{out_dir}`. A subprocess rather than a Python package:
the tool can then be swapped or updated without a rebuild.

Subtitle files are expected to be named `<anything>.<lang>.<ext>`; with
MINDBACKUP_STT_LANGUAGE set, the file in that language wins.
"""

from __future__ import annotations

import html
import logging
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .stt import TranscriptionError

logger = logging.getLogger(__name__)

SUBTITLE_SUFFIXES = (".vtt", ".srt")

_SUBTITLE_TAG = re.compile(r"<[^>]+>")


class VideoError(TranscriptionError):
    """No transcript could be had for the link. Shown to the user in Telegram."""


@dataclass(frozen=True)
class VideoTranscript:
    url: str
    text: str
    language: str | None
    tool: str


def is_video_url(text: str, settings: Settings) -> bool:
    """True when the whole message matches MINDBACKUP_VIDEO_URL_PATTERN."""
    return re.fullmatch(settings.video_url_pattern, text.strip(), re.IGNORECASE) is not None


def build_command(template: str, url: str, out_dir: str) -> list[str]:
    """The configured command as argv, placeholders filled in.

    Split before substituting, so a URL is always exactly one argument. The
    URL goes last when the template doesn't place it.
    """
    args = shlex.split(template)
    if not any("{url}" in arg for arg in args):
        args.append(url)
    return [arg.replace("{url}", url).replace("{out_dir}", out_dir) for arg in args]


def _subtitle_language(path: Path) -> str | None:
    """`<name>.<lang>.<ext>`: the language is the second-to-last suffix."""
    return path.suffixes[-2].lstrip(".") if len(path.suffixes) >= 2 else None


def _preference(path: Path, language: str | None) -> tuple[int, str]:
    """Sort key over the subtitle files the command wrote; lowest wins."""
    best = language is not None and _subtitle_language(path) == language
    return (0 if best else 1, path.name)


def subtitles_to_text(subtitles: str) -> str:
    """The spoken words out of a WebVTT or SRT file, in one paragraph.

    Automatic captions roll: each cue repeats the previous line before adding
    the next one, so a line equal to the last one kept is dropped.
    """
    lines: list[str] = []
    in_block = False
    for raw in subtitles.splitlines():
        line = raw.strip()
        if not line:
            in_block = False
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", "REGION")):
            in_block = line.startswith(("NOTE", "STYLE", "REGION"))
            continue
        if in_block or "-->" in line or line.isdigit():
            continue
        line = html.unescape(_SUBTITLE_TAG.sub("", line)).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return " ".join(lines)


def fetch_transcript(url: str, settings: Settings) -> VideoTranscript:
    """Run the configured command for the video's subtitles, flatten them to text.

    Raises VideoError with a message fit for the user.
    """
    if not settings.video_command:
        raise VideoError("Video links are off. Set MINDBACKUP_VIDEO_COMMAND to turn them on.")

    url = url.strip()
    timeout = settings.video_timeout
    with tempfile.TemporaryDirectory(prefix="mindbackup-video-") as tmp:
        command = build_command(settings.video_command, url, tmp)
        tool = Path(command[0]).name
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise VideoError(f"{command[0]!r} not found. Check MINDBACKUP_VIDEO_COMMAND.") from exc
        except subprocess.TimeoutExpired as exc:
            raise VideoError(f"{tool} took longer than {timeout:g}s for {url}.") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            logger.warning("%s failed for %s: %s", tool, url, completed.stderr)
            raise VideoError(f"{tool} failed: {detail[-1] if detail else completed.returncode}")

        subtitles = sorted(p for p in Path(tmp).iterdir() if p.suffix in SUBTITLE_SUFFIXES)
        if not subtitles:
            raise VideoError("That video has no transcript to fetch.")
        chosen = min(subtitles, key=lambda path: _preference(path, settings.stt_language))
        text = subtitles_to_text(chosen.read_text(encoding="utf-8", errors="replace"))

    if not text:
        raise VideoError("That video's transcript is empty.")

    language = _subtitle_language(chosen)
    logger.info("Fetched video transcript for %s (%d chars, lang=%s)", url, len(text), language)
    return VideoTranscript(url=url, text=text, language=language, tool=tool)

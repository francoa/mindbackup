"""Tests for video-link ingest: link detection, subtitle flattening, and the command call.

The fetch command is a small script that writes the subtitle files a real
tool would, so these run offline.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from mindbackup.config import Settings
from mindbackup.pipeline import ingest_video
from mindbackup.video import (
    VideoError,
    build_command,
    fetch_transcript,
    is_video_url,
    subtitles_to_text,
)

ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000 align:start position:0%
hello<00:00:00.500><c> there</c>

00:00:02.000 --> 00:00:04.000 align:start position:0%
hello there
general &amp; kenobi

00:00:04.000 --> 00:00:06.000
general &amp; kenobi
"""

SRT = """1
00:00:00,000 --> 00:00:02,000
hello there

2
00:00:02,000 --> 00:00:04,000
<i>general kenobi</i>
"""

URL = "https://videos.example/watch?v=abc123"


@pytest.mark.parametrize("text", [URL, "  http://videos.example/abc  "])
def test_links_match_the_default_pattern(text):
    assert is_video_url(text, Settings(vault_path=Path("/v")))


@pytest.mark.parametrize("text", ["remember to buy milk", f"watch {URL} later"])
def test_other_text_is_not_a_video_link(text):
    assert not is_video_url(text, Settings(vault_path=Path("/v")))


def test_the_pattern_is_configurable():
    settings = Settings(vault_path=Path("/v"), video_url_pattern=r"https://videos\.example/\S+")
    assert is_video_url(URL, settings)
    assert not is_video_url("https://elsewhere.example/abc", settings)


def test_vtt_is_flattened_without_rolling_repeats():
    assert subtitles_to_text(ROLLING_VTT) == "hello there general & kenobi"


def test_srt_is_flattened():
    assert subtitles_to_text(SRT) == "hello there general kenobi"


def test_placeholders_are_filled_in_whole_arguments():
    command = build_command(
        'tool -o "{out_dir}/%(id)s.%(ext)s" -- {url}', "https://x/a b", "/tmp/o"
    )
    assert command == ["tool", "-o", "/tmp/o/%(id)s.%(ext)s", "--", "https://x/a b"]


def test_url_is_appended_when_the_template_does_not_place_it():
    assert build_command("tool --out {out_dir}", URL, "/tmp/o") == ["tool", "--out", "/tmp/o", URL]


def _fake_fetcher(tmp_path: Path, files: dict[str, str], exit_code: int = 0) -> str:
    """A command template running a stand-in that writes `files` into `{out_dir}`."""
    script = tmp_path / "fetch-subs"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        f"for name, body in {files!r}.items():\n"
        "    (out / name).write_text(body)\n"
        f"if {exit_code}:\n"
        "    print('ERROR: Video unavailable', file=sys.stderr)\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return f"{script} -o {{out_dir}} -- {{url}}"


def _settings(tmp_path: Path, command: str, language: str | None = None) -> Settings:
    return Settings(vault_path=tmp_path / "vault", video_command=command, stt_language=language)


def test_fetch_takes_the_first_track_by_name(tmp_path):
    command = _fake_fetcher(
        tmp_path, {"abc123.fr.vtt": "WEBVTT\n\nsalut\n", "abc123.es.vtt": "WEBVTT\n\nhola\n"}
    )
    video = fetch_transcript(URL, _settings(tmp_path, command))
    assert (video.text, video.language, video.tool) == ("hola", "es", "fetch-subs")


def test_fetch_prefers_the_configured_language(tmp_path):
    command = _fake_fetcher(
        tmp_path, {"abc123.es.vtt": "WEBVTT\n\nhola\n", "abc123.fr.srt": "1\n\nsalut\n"}
    )
    video = fetch_transcript(URL, _settings(tmp_path, command, language="fr"))
    assert (video.text, video.language) == ("salut", "fr")


def test_fetch_without_a_command_says_how_to_turn_it_on(tmp_path):
    with pytest.raises(VideoError, match="MINDBACKUP_VIDEO_COMMAND"):
        fetch_transcript(URL, _settings(tmp_path, ""))


def test_fetch_without_subtitles_fails_clearly(tmp_path):
    with pytest.raises(VideoError, match="no transcript"):
        fetch_transcript(URL, _settings(tmp_path, _fake_fetcher(tmp_path, {})))


def test_fetch_surfaces_the_command_error(tmp_path):
    command = _fake_fetcher(tmp_path, {}, exit_code=1)
    with pytest.raises(VideoError, match="Video unavailable"):
        fetch_transcript(URL, _settings(tmp_path, command))


def test_missing_binary_names_the_setting(tmp_path):
    with pytest.raises(VideoError, match="MINDBACKUP_VIDEO_COMMAND"):
        fetch_transcript(URL, _settings(tmp_path, f"{tmp_path / 'nope'} {{url}}"))


def test_ingest_writes_a_memo_with_the_link(tmp_path):
    command = _fake_fetcher(tmp_path, {"abc123.en.vtt": ROLLING_VTT})
    result = ingest_video(URL, _settings(tmp_path, command))

    memo = result.memo.path.read_text()
    assert "source: video\n" in memo
    assert f"url: {URL}\n" in memo
    assert memo.rstrip().endswith("hello there general & kenobi")
    assert result.transcript.text == "hello there general & kenobi"
    assert result.transcript.model == "fetch-subs"
    assert result.archived_audio is None

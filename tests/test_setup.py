"""scripts/setup.sh, driven with piped answers against a throwaway root.

Runs with --offline, so nothing here talks to Telegram, an LLM or GitHub.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from mindbackup.config import _get_from_secrets, load_env_file

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "setup.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

ALL_BUT_PREREQS_AND_FINISH = [
    "env",
    "telegram",
    "users",
    "vault",
    "llm",
    "stt",
    "timezone",
    "video",
]


def run_setup(tmp_path: Path, steps: list[str], answers: list[str]) -> subprocess.CompletedProcess:
    args = ["bash", str(SCRIPT), "--offline"]
    for step in steps:
        args += ["--step", step]
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "MINDBACKUP_SETUP_ROOT": str(tmp_path / "repo"),
        "MINDBACKUP_SECRETS_DIR": str(tmp_path / "secrets"),
    }
    return subprocess.run(
        args, input="\n".join(answers) + "\n", capture_output=True, text=True, env=env, timeout=30
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "home").mkdir()
    (repo / ".env.example").write_text(
        "# template\nMINDBACKUP_TELEGRAM_TOKEN=\nMINDBACKUP_STT_MODEL=medium\n", encoding="utf-8"
    )
    return repo


def full_run(tmp_path: Path) -> subprocess.CompletedProcess:
    return run_setup(
        tmp_path,
        ALL_BUT_PREREQS_AND_FINISH,
        [
            # telegram: token, store in secret file
            "123456:ABC-def_ghi",
            "",
            # users: manual, then the ids
            "2",
            "111, 222",
            # vault: path (missing, so confirm creating it), memo dir, topic dir, archive
            str(tmp_path / "vault"),
            "",
            "",
            "Journal",
            "./archive",
            # llm: enable, base url, key, store in .env, model
            "y",
            "",
            "sk-test-key-123456",
            "2",
            "",
            # stt: a bad model is asked again; language; vocabulary
            "bogus",
            "small",
            "es",
            "Padel, Obsidian",
            # timezone
            "Europe/Madrid",
            # video: on, with the default command, pattern and timeout
            "2",
            "",
            "",
            "",
        ],
    )


def test_full_run_writes_env_and_secrets(tmp_path: Path, repo: Path):
    result = full_run(tmp_path)
    assert result.returncode == 0, result.stderr

    env = load_env_file(repo / ".env")
    assert env["UID"] == str(os.getuid())
    assert env["MINDBACKUP_ALLOWED_USERS"] == "111,222"
    assert env["VAULT_PATH"] == str(tmp_path / "vault")
    assert env["MINDBACKUP_MEMO_DIR"] == "Memos"
    assert env["MINDBACKUP_TOPIC_DIR"] == "Journal"
    assert env["ARCHIVE_PATH"] == str(repo / "archive")
    assert env["MINDBACKUP_LLM_BASE_URL"] == "https://api.anthropic.com/v1"
    assert env["MINDBACKUP_LLM_API_KEY"] == "sk-test-key-123456"
    assert env["MINDBACKUP_LLM_MODEL"] == "claude-haiku-4-5"
    assert env["MINDBACKUP_STT_MODEL"] == "small"
    assert env["MINDBACKUP_MEMORY_LIMIT"] == "2g"
    assert env["MINDBACKUP_STT_LANGUAGE"] == "es"
    assert env["MINDBACKUP_VOCABULARY"] == "Padel, Obsidian"
    assert env["MINDBACKUP_TIMEZONE"] == "Europe/Madrid"
    # The yt-dlp default, its quotes and placeholders surviving the round trip
    # through .env. Flags are left out so tuning them doesn't break this.
    command = env["MINDBACKUP_VIDEO_COMMAND"]
    assert command.startswith("/app/bin/yt-dlp ")
    assert '--sub-langs ".*-orig,' in command
    assert command.endswith('-o "{out_dir}/%(id)s.%(ext)s" -- {url}')
    assert env["MINDBACKUP_VIDEO_URL_PATTERN"] == (
        r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?\S*v=|shorts/|live/)"
        r"|youtu\.be/)[\w-]{11}\S*"
    )
    assert env["MINDBACKUP_VIDEO_TIMEOUT"] == "240"
    # Moved to the secret file, so it must not linger in .env.
    assert "MINDBACKUP_TELEGRAM_TOKEN" not in env

    assert (tmp_path / "vault").is_dir()
    assert (repo / "archive").is_dir()

    secrets = tmp_path / "secrets"
    assert (secrets / "token.txt").read_text().strip() == "123456:ABC-def_ghi"
    # Stored in .env, but compose still needs the file to exist.
    assert (secrets / "llm-api-key.txt").read_text().strip() == ""
    for path in (secrets / "token.txt", secrets / "llm-api-key.txt", repo / ".env"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path

    # Secrets are typed hidden and never echoed back in full.
    assert "sk-test-key-123456" not in result.stdout + result.stderr
    assert "ABC-def_ghi" not in result.stdout + result.stderr


def test_rerun_keeps_values_on_enter_and_does_not_duplicate_keys(tmp_path: Path, repo: Path):
    assert full_run(tmp_path).returncode == 0
    before = (repo / ".env").read_text()

    result = run_setup(tmp_path, ["vault", "stt"], [""] * 7)
    assert result.returncode == 0, result.stderr

    after = (repo / ".env").read_text()
    assert after == before
    keys = [line.split("=", 1)[0] for line in after.splitlines() if "=" in line]
    assert len(keys) == len(set(keys))
    assert (repo / ".env.bak").is_file()


def test_rejects_non_numeric_user_ids_until_valid(tmp_path: Path, repo: Path):
    result = run_setup(tmp_path, ["env", "users"], ["2", "@me", "42"])
    assert result.returncode == 0, result.stderr
    assert load_env_file(repo / ".env")["MINDBACKUP_ALLOWED_USERS"] == "42"


def test_skipping_llm_still_creates_the_secret_file(tmp_path: Path, repo: Path):
    result = run_setup(tmp_path, ["env", "llm"], ["n"])
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "secrets" / "llm-api-key.txt").read_text().strip() == ""


def test_custom_video_command_rejects_a_bad_regex(tmp_path: Path, repo: Path):
    result = run_setup(
        tmp_path,
        ["env", "video"],
        ["2", "fetch-subs {url} {out_dir}", "https?://(", r"https?://\S+", "45"],
    )
    assert result.returncode == 0, result.stderr
    env = load_env_file(repo / ".env")
    assert env["MINDBACKUP_VIDEO_COMMAND"] == "fetch-subs {url} {out_dir}"
    assert env["MINDBACKUP_VIDEO_URL_PATTERN"] == r"https?://\S+"
    assert env["MINDBACKUP_VIDEO_TIMEOUT"] == "45"


def test_video_rerun_keeps_a_custom_command_on_enter(tmp_path: Path, repo: Path):
    assert run_setup(tmp_path, ["env", "video"], ["2", "fetch-subs {url}", "", ""]).returncode == 0
    before = load_env_file(repo / ".env")

    # Any configured command means the default choice is "On", not a loop.
    result = run_setup(tmp_path, ["video"], ["", "", "", ""])
    assert result.returncode == 0, result.stderr
    assert load_env_file(repo / ".env") == before
    assert before["MINDBACKUP_VIDEO_COMMAND"] == "fetch-subs {url}"


def test_video_off_clears_the_command(tmp_path: Path, repo: Path):
    assert run_setup(tmp_path, ["env", "video"], ["2", "fetch-subs {url}", "", ""]).returncode == 0
    result = run_setup(tmp_path, ["video"], ["1"])
    assert result.returncode == 0, result.stderr
    assert load_env_file(repo / ".env")["MINDBACKUP_VIDEO_COMMAND"] == ""


def test_unknown_step_fails(tmp_path: Path, repo: Path):
    result = run_setup(tmp_path, ["nope"], [])
    assert result.returncode != 0
    assert "Unknown step" in result.stderr


def test_list_names_every_step():
    result = subprocess.run(["bash", str(SCRIPT), "--list"], capture_output=True, text=True)
    assert result.returncode == 0
    for step in ["prereqs", *ALL_BUT_PREREQS_AND_FINISH, "finish"]:
        assert step in result.stdout


def test_empty_secret_file_falls_back_to_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MINDBACKUP_LLM_API_KEY", "from-env")
    (tmp_path / "llm_api_key").write_text("\n")
    assert _get_from_secrets("llm_api_key", "MINDBACKUP_LLM_API_KEY", tmp_path) == "from-env"

    (tmp_path / "llm_api_key").write_text("from-file\n")
    assert _get_from_secrets("llm_api_key", "MINDBACKUP_LLM_API_KEY", tmp_path) == "from-file"

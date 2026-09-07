from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mindbackup.__main__ import main  # noqa: E402
from mindbackup.config import (  # noqa: E402
    DEFAULT_MODELS,
    VALID_PROVIDERS,
    ConfigError,
    Settings,
    _parse_allowed_users,
    load_settings,
)

from mindbackup.pipeline import resolve_memo_date  # noqa: E402
from mindbackup.stt import build_prompt  # noqa: E402

from mindbackup.vault import (  # noqa: E402
    VaultWriteError,
    render_memo,
    write_memo,
)


def settings_for(tmp_path: Path, **overrides) -> Settings:
    base: dict = dict(vault_path=tmp_path, memo_dir="Memos", timezone="Europe/Madrid")
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- rendering ------------------------------------------------------------


def test_render_has_exact_m1_frontmatter():
    out = render_memo("hello world", date(2026, 9, 4))
    assert out.startswith(
        "---\ndate: 2026-09-04\ntype: memo\nsource: telegram\n---\n\n"
    )
    assert out.endswith("hello world\n")


def test_render_contains_nothing_but_frontmatter_and_transcript():
    out = render_memo("only this", date(2026, 9, 4))
    body = out.split("---\n", 2)[2].strip()
    assert body == "only this"


# --- writing --------------------------------------------------------------


def test_write_creates_expected_filename(tmp_path):
    memo = write_memo("keep the wrist firm today ok", date(2026, 9, 4), tmp_path / "Memos")
    assert memo.path.name == "2026-09-04.md"
    assert memo.path.parent.name == "Memos"


def test_write_creates_memo_dir(tmp_path):
    target = tmp_path / "Memos"
    assert not target.exists()
    write_memo("hello", date(2026, 9, 4), target)
    assert target.is_dir()


def test_second_memo_on_same_day_does_not_overwrite(tmp_path):
    """Several memos a day is the normal case, not an edge case."""
    memo_dir = tmp_path / "Memos"
    first = write_memo("same words here", date(2026, 9, 4), memo_dir)
    second = write_memo("different words entirely", date(2026, 9, 4), memo_dir)
    third = write_memo("a third memo", date(2026, 9, 4), memo_dir)

    assert first.path.name == "2026-09-04.md"
    assert second.path.name == "2026-09-04_2.md"
    assert third.path.name == "2026-09-04_3.md"
    # C4: the raw layer is immutable — earlier memos survive untouched.
    assert "same words here" in first.path.read_text(encoding="utf-8")
    assert "different words entirely" in second.path.read_text(encoding="utf-8")


def test_empty_transcript_is_refused(tmp_path):
    with pytest.raises(VaultWriteError):
        write_memo("   ", date(2026, 9, 4), tmp_path / "Memos")


def test_written_file_contains_full_transcript(tmp_path):
    transcript = "the coach said " + "swing through the ball. " * 200
    memo = write_memo(transcript, date(2026, 9, 4), tmp_path / "Memos")
    assert transcript.strip() in memo.path.read_text(encoding="utf-8")


def test_distinctive_phrase_is_greppable(tmp_path):
    """Acceptance criterion 4: search finds a distinctive spoken phrase."""
    memo_dir = tmp_path / "Memos"
    write_memo("remember to bend the knees on the bandeja", date(2026, 9, 3), memo_dir)
    hits = [p for p in memo_dir.glob("*.md") if "bandeja" in p.read_text(encoding="utf-8")]
    assert len(hits) == 1


# --- dates ----------------------------------------------------------------


def test_memo_date_uses_configured_timezone(tmp_path):
    """23:30 UTC on the 3rd is already the 4th in Madrid."""
    recorded = datetime(2026, 9, 3, 23, 30, tzinfo=timezone.utc)
    assert resolve_memo_date(settings_for(tmp_path), recorded) == date(2026, 9, 4)


def test_unknown_timezone_falls_back_without_crashing(tmp_path):
    recorded = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    settings = settings_for(tmp_path, timezone="Mars/Olympus")
    assert isinstance(resolve_memo_date(settings, recorded), date)


# --- config ---------------------------------------------------------------


def test_allowed_users_parses_list():
    assert _parse_allowed_users("111, 222 ,333") == frozenset({111, 222, 333})


# --- vocabulary prompt ----------------------------------------------------


def test_prompt_gets_a_terminating_period():
    """Without one, Whisper mirrors the style and drops ALL sentence
    punctuation from the transcript. Verified against real audio."""
    assert build_prompt("padel, bandeja") == "padel, bandeja."


@pytest.mark.parametrize("ending", [".", "!", "?"])
def test_prompt_keeps_existing_terminator(ending):
    assert build_prompt(f"padel, bandeja{ending}") == f"padel, bandeja{ending}"


def test_empty_vocabulary_yields_no_prompt():
    assert build_prompt("") is None
    assert build_prompt("   ") is None


def test_shipped_env_example_vocabulary_is_punctuated():
    """The default that ships must not silently degrade transcripts."""
    env_path = Path(__file__).resolve().parents[1] / ".env.example"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MINDBACKUP_VOCABULARY="):
            value = line.split("=", 1)[1]
            prompt = build_prompt(value)
            assert prompt is not None and prompt.endswith(".")
            break
    else:
        pytest.fail("MINDBACKUP_VOCABULARY missing from .env.example")



# --- regressions ----------------------------------------------------------


def test_valid_providers_is_a_tuple_not_a_string():
    """`("local")` is a str, so validation degrades to substring matching
    and `MINDBACKUP_STT_PROVIDER=l` would sail through."""
    assert isinstance(VALID_PROVIDERS, tuple)
    assert "l" not in VALID_PROVIDERS
    assert "local" in VALID_PROVIDERS


def test_every_valid_provider_has_a_default_model():
    assert set(VALID_PROVIDERS) <= set(DEFAULT_MODELS)


def test_unknown_provider_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("MINDBACKUP_STT_PROVIDER", "l")
    with pytest.raises(ConfigError, match="Unknown"):
        load_settings()


def test_ingest_of_missing_file_reports_cleanly(tmp_path, monkeypatch, capsys):
    """The error path must not crash — a broken error path is how a failure
    goes silent (spec §5.6)."""
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path))
    exit_code = main(["ingest", str(tmp_path / "nope.ogg")])
    assert exit_code == 1
    assert "No such file" in capsys.readouterr().err


def test_bad_date_reports_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path))
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"not really audio")
    assert main(["ingest", str(audio), "--date", "4th of July"]) == 1
    assert "ISO format" in capsys.readouterr().err


def test_missing_vault_config_reports_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    monkeypatch.setattr("mindbackup.config._project_env", dict)
    assert main(["doctor"]) == 2
    assert "OBSIDIAN_VAULT_PATH" in capsys.readouterr().err


def test_doctor_writes_report_to_stdout(tmp_path, monkeypatch, capsys):
    """`doctor` is a report, so it belongs on stdout where it can be piped."""
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path))
    main(["doctor"])
    assert "Vault" in capsys.readouterr().out


def test_allowed_users_rejects_garbage():
    with pytest.raises(ConfigError):
        _parse_allowed_users("111,notanid")


def test_bot_refuses_empty_allowlist(tmp_path):
    settings = settings_for(tmp_path, telegram_token="x", allowed_users=frozenset())
    with pytest.raises(ConfigError, match="ALLOWED_USERS"):
        settings.validate_for_bot()


def test_bot_refuses_missing_token(tmp_path):
    settings = settings_for(tmp_path, allowed_users=frozenset({1}))
    with pytest.raises(ConfigError, match="TELEGRAM_TOKEN"):
        settings.validate_for_bot()

"""Configuration: environment in, validated Settings out.

Deliberately dependency-free (no pydantic, no dotenv package) — this is a
two-user personal tool and every dependency is a thing that can break the
ingest path at 22:00 on a Thursday.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_ENV_FILE_CACHE: dict[str, str] | None = None

VALID_PROVIDERS = ("local",)

DEFAULT_MODELS = {
    "local": "medium",
}


class ConfigError(Exception):
    """Raised when configuration is missing or nonsensical."""


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE .env file. Ignores comments and blank lines."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _project_env() -> dict[str, str]:
    """The project's own .env, cached. Real environment always wins over it."""
    global _ENV_FILE_CACHE
    if _ENV_FILE_CACHE is None:
        root = Path(__file__).resolve().parents[2]
        _ENV_FILE_CACHE = load_env_file(root / ".env")
    return _ENV_FILE_CACHE


def _get(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    if value is None:
        value = _project_env().get(key, default)
    return value.strip()


def _get_from_secrets(secret_name: str, fallback_env_var: str = "") -> str:
    secret_path = Path(f"/run/secrets/{secret_name}")
    if secret_path.exists():
        return secret_path.read_text().strip()
    
    # Fallback to env var for local non-docker testing
    return _get(fallback_env_var)

@dataclass(frozen=True)
class Settings:
    vault_path: Path
    memo_dir: str = "Memos"
    stt_provider: str = "local"
    stt_model: str = "base"
    stt_language: str | None = None
    vocabulary: str = ""
    telegram_token: str = ""
    allowed_users: frozenset[int] = field(default_factory=frozenset)
    audio_archive: Path | None = None
    timezone: str | None = None
    topic_dir: str = "Topics"
    llm_base_url: str = "https://api.anthropic.com/v1"
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout: float = 60.0

    @property
    def memo_path(self) -> Path:
        return self.vault_path / self.memo_dir

    @property
    def topic_path(self) -> Path:
        return self.vault_path / self.topic_dir

    @property
    def state_path(self) -> Path:
        """Derived-data sidecar. Dot-prefixed so Obsidian ignores it.

        C4: everything here is re-derivable from the raw transcripts, so it is
        safe to delete and regenerate.
        """
        return self.vault_path / ".mindbackup"

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    def validate_for_llm(self) -> None:
        """Checks that only matter when actually calling the model."""
        if not self.llm_model:
            raise ConfigError(
                "MINDBACKUP_LLM_MODEL is not set. Pick a model id from your "
                "provider (e.g. claude-haiku-4-5 on the Claude API)."
            )
        if not self.llm_api_key:
            raise ConfigError(
                "MINDBACKUP_LLM_API_KEY is not set. Extraction needs an LLM; "
                "put a key for MINDBACKUP_LLM_BASE_URL in .env."
            )

    def validate_for_bot(self) -> None:
        """Checks that only matter when actually running the Telegram bot."""
        if not self.telegram_token:
            raise ConfigError(
                "MINDBACKUP_TELEGRAM_TOKEN is not set. Create a bot with "
                "@BotFather and put the token in .env (see .env.example)."
            )
        if not self.allowed_users:
            raise ConfigError(
                "MINDBACKUP_ALLOWED_USERS is empty. Refusing to start an "
                "open bot that writes to your vault. Set your numeric Telegram "
                "user id (ask @userinfobot) in .env."
            )


def _parse_allowed_users(raw: str) -> frozenset[int]:
    users: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            users.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"MINDBACKUP_ALLOWED_USERS contains a non-numeric entry: {chunk!r}. "
                "Telegram user ids are integers."
            ) from exc
    return frozenset(users)


def load_settings() -> Settings:
    """Build Settings from the environment. Raises ConfigError on bad input."""
    vault_raw = _get("OBSIDIAN_VAULT_PATH")
    if not vault_raw:
        raise ConfigError(
            "OBSIDIAN_VAULT_PATH is not set. Point it at your Obsidian vault."
        )
    vault_path = Path(vault_raw).expanduser()

    provider = (_get("MINDBACKUP_STT_PROVIDER") or "local").lower()
    if provider not in VALID_PROVIDERS:
        raise ConfigError(
            f"Unknown MINDBACKUP_STT_PROVIDER {provider!r}. "
            f"Valid options: {', '.join(VALID_PROVIDERS)}."
        )

    archive_raw = _get("MINDBACKUP_AUDIO_ARCHIVE")

    llm_timeout_raw = _get("MINDBACKUP_LLM_TIMEOUT")
    try:
        llm_timeout = float(llm_timeout_raw) if llm_timeout_raw else 60.0
    except ValueError as exc:
        raise ConfigError(
            f"MINDBACKUP_LLM_TIMEOUT must be a number of seconds, got {llm_timeout_raw!r}."
        ) from exc

    return Settings(
        vault_path=vault_path,
        memo_dir=_get("MINDBACKUP_MEMO_DIR") or "Memos",
        stt_provider=provider,
        stt_model=_get("MINDBACKUP_STT_MODEL") or DEFAULT_MODELS[provider],
        stt_language=_get("MINDBACKUP_STT_LANGUAGE") or None,
        vocabulary=_get("MINDBACKUP_VOCABULARY"),
        telegram_token=_get_from_secrets("bot_token", "MINDBACKUP_TELEGRAM_TOKEN"),
        allowed_users=_parse_allowed_users(_get("MINDBACKUP_ALLOWED_USERS")),
        audio_archive=Path(archive_raw).expanduser() if archive_raw else None,
        timezone=_get("MINDBACKUP_TIMEZONE") or None,
        topic_dir=_get("MINDBACKUP_TOPIC_DIR") or "Topics",
        llm_base_url=(
            _get("MINDBACKUP_LLM_BASE_URL") or "https://api.anthropic.com/v1"
        ).rstrip("/"),
        llm_model=_get("MINDBACKUP_LLM_MODEL"),
        llm_api_key=_get_from_secrets("llm_api_key", "MINDBACKUP_LLM_API_KEY"),
        llm_timeout=llm_timeout,
    )

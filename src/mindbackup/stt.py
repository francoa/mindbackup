"""Speech-to-text backends behind one tiny interface.

Adding a provider = one function + one registry entry. Milestone 1 ships
local faster-whisper as the default (private, no key).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Settings


class TranscriptionError(Exception):
    """Transcription failed. The message is shown to the user in Telegram."""


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    provider: str
    model: str


_LOCAL_MODEL_CACHE: dict[tuple[str, str], object] = {}


def build_prompt(vocabulary: str) -> str | None:
    """Normalise the vocabulary hint into a Whisper `initial_prompt`.

    Whisper continues the *style* of its prompt, so a prompt with no sentence
    punctuation makes it emit unpunctuated text. Measured on a real memo:
    a bare list yields "physio session today She said my lower back pain…",
    the same list with a trailing period yields "Physio session today. She
    said my lower back pain…". Always terminate the hint.
    """
    hint = (vocabulary or "").strip()
    if not hint:
        return None
    if hint[-1] not in ".!?":
        hint += "."
    return hint



def transcribe(audio_path: Path, settings: Settings) -> Transcript:
    """Transcribe audio using the configured provider."""
    if not audio_path.is_file():
        raise TranscriptionError(f"Audio file not found: {audio_path}")
    if audio_path.stat().st_size == 0:
        raise TranscriptionError("Audio file is empty (0 bytes).")

    backends = {
        "local": _transcribe_local,
    }
    return backends[settings.stt_provider](audio_path, settings)


# --------------------------------------------------------------------------
# local: faster-whisper
# --------------------------------------------------------------------------


def _transcribe_local(audio_path: Path, settings: Settings) -> Transcript:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError(
            "Local speech-to-text needs faster-whisper, which is not installed. "
            "Run: pip install 'voice-mind-backup[local]'"
        ) from exc

    key = (settings.stt_model, "cpu")
    model = _LOCAL_MODEL_CACHE.get(key)
    if model is None:
        try:
            model = WhisperModel(settings.stt_model, device="cpu", compute_type="int8")
        except Exception as exc:  # model download / bad name / no disk
            raise TranscriptionError(
                f"Could not load local Whisper model {settings.stt_model!r}: {exc}"
            ) from exc
        _LOCAL_MODEL_CACHE[key] = model

    try:
        segments, info = model.transcribe(  # type: ignore[attr-defined]
            str(audio_path),
            language=settings.stt_language,
            vad_filter=True,
            initial_prompt=build_prompt(settings.vocabulary),
        )
        text = "".join(segment.text for segment in segments).strip()
    except Exception as exc:
        raise TranscriptionError(f"Local transcription failed: {exc}") from exc

    if not text:
        raise TranscriptionError(
            "Transcription came back empty — the recording may be silent or too short."
        )
    return Transcript(
        text=text,
        language=getattr(info, "language", None),
        provider="local",
        model=settings.stt_model,
    )

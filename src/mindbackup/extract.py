"""The intelligence layer: transcript in, atoms out.

An *atom* is one meaningful, self-contained sentence lifted from a rambling
transcript, tagged with the topics it belongs to. Two jobs, one model call:

  1. Summarise — drop filler, keep the sentences that carry meaning.
  2. Resolve referents — "this project" becomes [[voice-mind-backup]], because
     the model is shown the topics that already exist in the vault plus what
     was talked about recently. This is the actual fix for "grep looks
     underwhelming": you search for a name you never said out loud.

Where a referent cannot be resolved confidently, the atom carries an
`ambiguity` and the caller asks the user — precision on demand, rather than a
category menu in front of every memo (spec C3: input friction is the thesis).

Nothing here mutates the raw transcript layer (C4).
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import Settings
from .llm import LLMError, complete_json

logger = logging.getLogger(__name__)

# Kinds are a fixed vocabulary because they drive retrieval ("show me todos"),
# unlike topics which are open-ended and accumulate (C7).
KINDS = ("todo", "decision", "idea", "fact", "question")
DEFAULT_KIND = "fact"

MAX_TRANSCRIPT_CHARS = 24000

SYSTEM_PROMPT = """\
You extract structured atoms from a voice-memo transcript. The speaker is \
thinking out loud, often mixing Spanish and English, and the transcript is \
unedited speech: false starts, repetition and filler are expected.

Return ONLY a JSON object of this shape:

{
  "atoms": [
    {
      "text": "one self-contained sentence, lightly cleaned up",
      "kind": "todo|decision|idea|fact|question",
      "topics": ["topic name", "..."],
      "confidence": 0.0-1.0,
      "ambiguity": "question to ask the user, or null"
    }
  ],
  "summary": "one sentence describing the memo as a whole"
}

Rules:
- Extract only sentences that carry meaning the speaker would want to find \
again. Skip greetings, filler, thinking-aloud and repetition. A 3-minute \
ramble often yields 2-5 atoms. Extracting nothing is a valid answer.
- Rewrite each atom to stand ALONE, out of context, months later. Resolve \
pronouns and deixis: "it", "this project", "that thing we discussed" must \
become the actual name.
- PREFER topics from the KNOWN TOPICS list when one fits; reuse beats \
inventing near-duplicates. Invent a new topic only for genuinely new subjects.
- Topic names are short human-readable noun phrases, lowercase, no "#".
- If a referent is genuinely unresolvable from the transcript and the known \
topics, keep the speaker's wording, set confidence below 0.5, and put a \
specific question in "ambiguity" (e.g. "Does 'the bot' mean voice-mind-backup \
or the padel booking bot?"). Do NOT guess silently.
- Preserve the speaker's original language in "text".
"""


@dataclass
class Atom:
    text: str
    kind: str = DEFAULT_KIND
    topics: list[str] = field(default_factory=list)
    confidence: float = 1.0
    ambiguity: str | None = None

    @property
    def needs_clarification(self) -> bool:
        return bool(self.ambiguity) or self.confidence < 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Atom":
        return cls(
            text=str(data.get("text", "")).strip(),
            kind=_clean_kind(data.get("kind")),
            topics=_clean_topics(data.get("topics")),
            confidence=_clean_confidence(data.get("confidence")),
            ambiguity=(str(data["ambiguity"]).strip() or None)
            if data.get("ambiguity")
            else None,
        )

    def get_text(self) -> str:
        # TODO: remove when ambiguity can be resolved by user input
        if self.needs_clarification:
            return f"{self.text} --- AMBIGUITY: {self.ambiguity}"
        return self.text


@dataclass
class Extraction:
    atoms: list[Atom]
    summary: str = ""

    @property
    def ambiguous(self) -> list[Atom]:
        return [a for a in self.atoms if a.needs_clarification]

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "atoms": [a.to_dict() for a in self.atoms]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Extraction":
        raw_atoms = data.get("atoms") or []
        atoms = [Atom.from_dict(a) for a in raw_atoms if isinstance(a, dict)]
        return cls(
            atoms=[a for a in atoms if a.text],
            summary=str(data.get("summary", "")).strip(),
        )


def _clean_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in KINDS else DEFAULT_KIND


def _clean_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def normalise_topic(name: str) -> str:
    """Human-readable topic name -> canonical form used for dedupe and links."""
    cleaned = re.sub(r"[#\[\]]", "", str(name or "")).strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -_/")


def _clean_topics(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    seen: dict[str, None] = {}
    for item in value:
        topic = normalise_topic(item)
        if topic:
            seen.setdefault(topic, None)
    return list(seen)


def build_user_prompt(transcript: str, known_topics: list[str]) -> str:
    text = transcript.strip()
    if len(text) > MAX_TRANSCRIPT_CHARS:
        # Truncate rather than fail: a long memo still yields useful atoms, and
        # the raw transcript remains complete in the vault.
        text = text[:MAX_TRANSCRIPT_CHARS] + "\n[transcript truncated]"

    topics_block = (
        "\n".join(f"- {t}" for t in known_topics) if known_topics else "(none yet)"
    )
    return (
        f"KNOWN TOPICS (reuse these names when they fit):\n{topics_block}\n\n"
        f"TRANSCRIPT:\n{text}"
    )


def extract_atoms(
    transcript: str,
    settings: Settings,
    known_topics: list[str] | None = None,
) -> Extraction:
    """Run the summariser + classifier over one transcript.

    Raises LLMError. Callers in the ingest path must catch it: a failed
    extraction is a degraded memo, never a lost one.
    """
    text = (transcript or "").strip()
    if not text:
        return Extraction(atoms=[], summary="")

    data = complete_json(
        settings,
        SYSTEM_PROMPT,
        build_user_prompt(text, known_topics or []),
    )
    if not isinstance(data, dict):
        raise LLMError(f"Expected a JSON object from the model, got {type(data).__name__}.")

    extraction = Extraction.from_dict(data)
    logger.info(
        "Extracted %d atom(s), %d needing clarification.",
        len(extraction.atoms),
        len(extraction.ambiguous),
    )
    return extraction


__all__ = ["Atom", "Extraction", "KINDS", "LLMError", "extract_atoms", "normalise_topic"]

"""Confirmation: an extraction that has not been committed yet.

The fourth pipeline stage — ingestion, transcription, extraction,
**confirmation**, saving — as a domain object rather than a Telegram detail.

A `Proposal` holds what the model extracted plus what the human decided about
it. Nothing reaches the vault until `commit()`, which is the only method here
that writes anything; everything above it is pure data.

No transport imports, by construction: no telegram, no argparse, no frontend.
That is what stops "the CLI files what the bot would have held back" from being
written a third time — every frontend drives this same object with the same
operations, and only the rendering differs.

Not persisted: a bot restart loses pending proposals, and that is the correct
trade (C4) — the memo is safe on disk and extraction is re-derivable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum

from .config import Settings
from .extract import Atom, Extraction, normalise_topic
from .topics import StoredAtom, file_atoms

logger = logging.getLogger(__name__)


class Verdict(str, Enum):
    """What a human decided about one atom.

    `REASSIGN` is an approval too, but it records that the *user* chose the
    topics rather than the model — which is exactly the case the old
    `--- AMBIGUITY:` string was standing in for.
    """

    APPROVE = "approve"
    REJECT = "reject"
    REASSIGN = "reassign"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    topics: tuple[str, ...] = ()


@dataclass
class Proposal:
    """Extraction output awaiting a human decision.

    An atom with no decision is *pending*: neither filed nor discarded. That is
    the thing the pipeline had nowhere to put before, and the reason ambiguous
    atoms used to be filed with their question glued onto the text.
    """

    memo_name: str
    memo_date: str
    extraction: Extraction
    audio: str | None = None
    decisions: dict[int, Decision] = field(default_factory=dict)

    @property
    def atoms(self) -> list[Atom]:
        return self.extraction.atoms

    @property
    def summary(self) -> str:
        return self.extraction.summary

    # --- queries the frontends need ---------------------------------------

    def pending(self) -> list[tuple[int, Atom]]:
        """Atoms still awaiting a decision, with the index to decide them by."""
        return [(i, atom) for i, atom in enumerate(self.atoms) if i not in self.decisions]

    def needs_clarification(self) -> list[tuple[int, Atom]]:
        """Pending atoms the model itself flagged as unresolved."""
        return [(i, atom) for i, atom in self.pending() if atom.needs_clarification]

    def approved(self) -> list[Atom]:
        """What `commit()` would file, with reassignments already applied.

        Returns copies: querying a proposal never mutates the extraction, so
        a frontend can render this before committing, or not commit at all.
        """
        approved: list[Atom] = []
        for index, atom in enumerate(self.atoms):
            decision = self.decisions.get(index)
            if decision is None or decision.verdict is Verdict.REJECT:
                continue
            if decision.verdict is Verdict.REASSIGN:
                atom = replace(
                    atom,
                    topics=list(decision.topics),
                    ambiguity=None,
                    confidence=1.0,
                )
            approved.append(atom)
        return approved

    def rejected(self) -> list[Atom]:
        return [
            atom
            for index, atom in enumerate(self.atoms)
            if (d := self.decisions.get(index)) is not None and d.verdict is Verdict.REJECT
        ]

    # --- the operations a human performs, transport-agnostic ---------------

    def approve(self, index: int) -> None:
        self._decide(index, Decision(Verdict.APPROVE))

    def approve_all(self) -> None:
        """File everything pending, ambiguity included.

        The batch CLI's policy, now a visible choice rather than an accident of
        `extract_memo`'s signature.
        """
        for index, _ in self.pending():
            self.approve(index)

    def approve_confident(self) -> None:
        """Approve what the model was sure of; leave the rest pending.

        The bot's one-tap policy: filing a wrong classification is worse than
        filing none (spec C6), so an unclear atom waits for a human instead.
        """
        for index, atom in self.pending():
            if not atom.needs_clarification:
                self.approve(index)

    def reject(self, index: int) -> None:
        self._decide(index, Decision(Verdict.REJECT))

    def reassign(self, index: int, topics: list[str]) -> None:
        """Answer the model's question: these are the topics, file it."""
        cleaned = tuple(dict.fromkeys(t for t in map(normalise_topic, topics) if t))
        self._decide(index, Decision(Verdict.REASSIGN, cleaned))

    def _decide(self, index: int, decision: Decision) -> None:
        if not 0 <= index < len(self.atoms):
            raise IndexError(f"No atom {index} in the proposal for {self.memo_name!r}.")
        self.decisions[index] = decision

    # --- the one method that touches the vault -----------------------------

    def commit(self, settings: Settings) -> list[StoredAtom]:
        """File the approved atoms. Pending and rejected ones are not written."""
        approved = self.approved()
        if not approved:
            logger.info("Nothing approved for %s; nothing filed.", self.memo_name)
            return []
        return file_atoms(
            approved,
            self.memo_name,
            self.memo_date or date.today(),
            settings,
            audio=self.audio,
        )


__all__ = ["Decision", "Proposal", "Verdict"]

"""Tests for the confirmation stage.

Migrated from `test_review.py`: these assertions used to be about
`PendingReview`, the Telegram-only object. They encode behaviour that must
survive the move into the domain — an unresolved atom is not filed, a resolved
one is, and the audio backref follows the atom into the index.

The new ones below are the point of the refactor: nothing reaches the vault
until `commit()`, and every frontend gets the same answer to "what would be
filed?".
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from mindbackup.config import Settings
from mindbackup.extract import Atom, Extraction
from mindbackup.proposal import Proposal, Verdict
from mindbackup.topics import iter_index, search


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        vault_path=tmp_path / "vault",
        llm_model="fake/model",
        llm_api_key="fake-key",
        allowed_users=frozenset({42}),
    )


def _atoms() -> list[Atom]:
    return [
        Atom(text="Fix search.", kind="todo", topics=["voice-mind-backup"], confidence=0.9),
        Atom(text="Buy grip.", kind="todo", topics=["padel"], confidence=0.9),
        Atom(
            text="Talk to him.", kind="todo", topics=[], confidence=0.2, ambiguity="Who is 'him'?"
        ),
    ]


def _proposal(**overrides) -> Proposal:
    base: dict = {
        "memo_name": "2026-09-06",
        "memo_date": "2026-09-06",
        "extraction": Extraction(atoms=_atoms(), summary="s"),
    }
    base.update(overrides)
    return Proposal(**base)  # type: ignore[arg-type]


# --- nothing is written until commit ---------------------------------------


def test_a_fresh_proposal_has_decided_nothing(settings):
    proposal = _proposal()

    assert len(proposal.pending()) == 3, "every atom starts awaiting a decision"
    assert proposal.approved() == []
    assert [index for index, _ in proposal.needs_clarification()] == [2]


def test_deciding_writes_nothing_to_the_vault(settings):
    """The whole reason the object exists: decisions are pure data."""
    proposal = _proposal()
    proposal.approve_all()
    proposal.reject(1)

    assert not settings.topic_path.exists(), "no page may appear before commit()"
    assert list(iter_index(settings)) == []


def test_commit_files_nothing_when_nothing_is_approved(settings):
    proposal = _proposal()
    proposal.reject(0)

    assert proposal.commit(settings) == []
    assert list(iter_index(settings)) == []


# --- what each policy files ------------------------------------------------


def test_approve_confident_holds_back_the_unclear_atom(settings):
    """The bot's one-tap policy. Was: `apply_review` filing `review.confident`."""
    proposal = _proposal()
    proposal.approve_confident()
    filed = proposal.commit(settings)

    assert len(filed) == 2, "the ambiguous atom must not be filed unresolved"
    assert {f.text for f in filed} == {"Fix search.", "Buy grip."}
    assert [index for index, _ in proposal.pending()] == [2], "it stays pending, not discarded"


def test_approve_all_files_the_unclear_atom_too(settings):
    """The batch CLI's policy: no one to ask, so file the lot — explicitly."""
    proposal = _proposal()
    proposal.approve_all()
    filed = proposal.commit(settings)

    assert len(filed) == 3
    unclear = next(f for f in filed if f.text == "Talk to him.")
    assert "AMBIGUITY" not in unclear.text, "the question must never reach the vault as prose"


def test_reject_keeps_an_atom_out_of_the_vault(settings):
    proposal = _proposal()
    proposal.approve_all()
    proposal.reject(1)
    filed = proposal.commit(settings)

    assert {f.text for f in filed} == {"Fix search.", "Talk to him."}
    assert [a.text for a in proposal.rejected()] == ["Buy grip."]


def test_reassign_resolves_the_ambiguity_and_files_it(settings):
    """Was: `review.resolved[2] = "coach"` — the user answering "who is him?"."""
    proposal = _proposal()
    proposal.approve_confident()
    proposal.reassign(2, ["coach"])

    filed = proposal.commit(settings)

    assert len(filed) == 3
    resolved = next(f for f in filed if f.text == "Talk to him.")
    assert resolved.topics == ["coach"]
    assert proposal.pending() == [], "a reassigned atom is decided"


def test_reassign_normalises_and_dedupes_what_the_user_typed(settings):
    proposal = _proposal()
    proposal.reassign(2, ["  Coach ", "#coach", "Lower Back"])

    assert proposal.decisions[2].topics == ("coach", "lower back")
    assert proposal.decisions[2].verdict is Verdict.REASSIGN


def test_queries_do_not_mutate_the_extraction(settings):
    """`approved()` is a query; rendering it must not change what was extracted."""
    proposal = _proposal()
    proposal.reassign(2, ["coach"])

    assert proposal.approved()[0].topics == ["coach"]
    assert proposal.atoms[2].topics == [], "the model's own output stays as it was"
    assert proposal.atoms[2].ambiguity == "Who is 'him'?"


def test_deciding_an_atom_that_does_not_exist_is_a_bug_not_a_silent_skip(settings):
    with pytest.raises(IndexError):
        _proposal().approve(7)


# --- filing details that must survive the move -----------------------------


def test_commit_records_the_audio_backref(settings):
    """The audio backref lives in the index, not on the topic page.

    The page shows only the memo link; the recording is named after the memo,
    so printing it there was the link repeated.
    """
    proposal = _proposal(
        extraction=Extraction(atoms=[Atom(text="Fix search.", topics=["voice-mind-backup"])]),
        audio="2026-09-06.ogg",
    )
    proposal.approve_all()
    filed = proposal.commit(settings)

    assert filed[0].audio == "2026-09-06.ogg", "must link back to the audio"
    page = (settings.topic_path / "voice-mind-backup.md").read_text(encoding="utf-8")
    assert ".ogg" not in page
    assert "[[2026-09-06]]" in page


def test_committed_atoms_are_searchable(settings):
    proposal = _proposal()
    proposal.approve_confident()
    proposal.commit(settings)

    assert len(search(settings, "grip")) == 1


def test_a_proposal_without_a_date_still_files(settings):
    """A memo whose name carries no date must not block filing."""
    proposal = _proposal(memo_date="")
    proposal.approve_confident()
    filed = proposal.commit(settings)

    assert filed[0].memo_date == date.today().isoformat()

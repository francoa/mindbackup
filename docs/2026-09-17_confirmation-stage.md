# Confirmation as a real pipeline stage

Status: proposed
Date: 2026-09-17

## The problem

The pipeline is documented as five stages — ingestion, transcription,
extraction, **confirmation**, saving. Four of them live in `pipeline.py`.
The fourth does not exist below the Telegram layer.

Trace "extract → confirm → save" through both frontends today:

| | CLI (`__main__.py:cmd_extract`) | Bot (`bot.py:_offer_extraction`) |
|---|---|---|
| entry point | `extract_memo(memo_path, settings, file=True)` | `extract_atoms(text, settings, known_topics(...))` — **bypasses `pipeline.py`** |
| confirmation | none; everything is filed | `PendingReview` in `bot_data`, keyed by Telegram `message_id` |
| filing | `file_atoms()` inside `extract_memo` | `apply_review()` → `file_atoms()` |
| ambiguous atoms | filed, with the ambiguity glued into the text | held back, offered for resolution |

Two consequences:

1. **The bot re-implements stage ordering that `pipeline.py` is supposed to
   own.** `_offer_extraction` calls `extract_atoms` directly because
   `extract_memo` insists on either filing everything or filing nothing.
2. **The same operation has different semantics per frontend.** An atom the
   bot would hold back for confirmation, the CLI files unconfirmed.

This is already visible as rot in the code:

- `pipeline.py:194-197` — a TODO disabling the `needs_clarification` filter,
  because at that point in the call stack there is nowhere to hold an atom
  pending a decision. The workaround is to file it anyway.
- `extract.py:101-105` — `Atom.get_text()` appends
  `" --- AMBIGUITY: ..."` to the stored text, marked
  `TODO: remove when ambiguity can be resolved by user input`. An unresolved
  domain concept leaking into the vault as a string.
- `review.py:PendingReview` mixes domain state (which atoms, which
  resolutions) with transport state (message-id keying, restart semantics).

A third frontend — an interactive CLI — becomes the third implementation of
confirmation, and the second place the TODOs get copied to.

## What is missing

Not an architecture. One domain object: **a proposal that has not been
committed yet**, with the operations a human performs on it.

```python
@dataclass
class Proposal:
    """Extraction output awaiting a human decision. Nothing is written
    until commit()."""
    memo_name: str
    memo_date: str
    extraction: Extraction
    audio: str | None = None

    decisions: dict[int, Decision]   # index -> approve / reject / reassign

    # queries the frontends need
    def pending(self) -> list[tuple[int, Atom]]: ...
    def approved(self) -> list[Atom]: ...
    def needs_clarification(self) -> list[tuple[int, Atom]]: ...

    # the operations a human performs, transport-agnostic
    def approve(self, index: int) -> None: ...
    def approve_all(self) -> None: ...
    def reject(self, index: int) -> None: ...
    def reassign(self, index: int, topics: list[str]) -> None: ...

    def commit(self, settings: Settings) -> list[StoredAtom]: ...
```

`commit()` is the only method that touches the vault, and it delegates to the
existing `file_atoms()`. Everything above it is pure data.

## Where it goes

New module `mindbackup/proposal.py` — domain, no transport imports.

`pipeline.py` grows one function alongside `extract_memo`:

```python
def propose_from_memo(memo_path, settings) -> Proposal
def propose_from_transcript(text, memo_name, memo_date, settings, *, audio=None) -> Proposal
```

The second one is what the bot needs: it already has the transcript in memory
after `ingest_audio` and should not re-read the memo off disk.

`extract_memo(..., file=True)` stays, expressed in terms of the new object:

```python
proposal = propose_from_memo(memo_path, settings)
proposal.approve_all()          # today's behaviour, now explicit
filed = proposal.commit(settings) if file else []
```

That keeps `cmd_extract` and its tests working unchanged, while making the
"file everything without asking" policy a visible choice rather than an
accident of the signature.

## What each frontend becomes

**Bot.** `PendingReview` is deleted; `bot_data["reviews"]` holds `Proposal`
objects keyed by message id. `_offer_extraction` calls
`propose_from_transcript` instead of `extract_atoms`, so it stops bypassing
the pipeline. `apply_review(review, settings)` becomes
`proposal.commit(settings)`. The Telegram-specific parts — rendering,
keyboards, callback data — stay in the Telegram layer (see the companion
note on the transport split).

**Batch CLI.** `cmd_extract` unchanged in behaviour. `--dry-run` becomes
"build the proposal, print it, never commit" — which is what the flag already
means, now enforced by the type rather than by a boolean threaded down two
call levels.

**Interactive CLI.** Roughly:

```python
proposal = propose_from_memo(path, settings)
for index, atom in proposal.pending():
    render(atom)
    match prompt():        # a/r/t
        case "a": proposal.approve(index)
        case "r": proposal.reject(index)
        case "t": proposal.reassign(index, ask_topics(known_topics(settings)))
proposal.commit(settings)
```

No new pipeline code. That is the test of whether this abstraction is the
right one.

## What this fixes for free

- `pipeline.py:194-197` — the held-back filter comes back, because there is
  now a place for a held-back atom to sit. Ambiguous atoms are `pending`
  rather than silently approved.
- `extract.py:get_text()` — the `--- AMBIGUITY:` string concatenation goes
  away. An unresolved atom either gets resolved via `reassign` or is not
  committed; its ambiguity never reaches the vault as prose.
- The bot's direct `extract_atoms` import disappears, so `pipeline.py`
  regains sole ownership of stage ordering.

## Scope

**In:** `proposal.py`; two `propose_*` functions in `pipeline.py`;
`extract_memo` reimplemented over them; bot migrated off `PendingReview`;
`--interactive` on `mindbackup extract`.

**Out:** frontend plugin registry, event bus, repository interface over the
vault, dependency injection. Two frontends and one user justify none of it.

**Out for now:** persisting proposals across restarts. Today a bot restart
loses pending reviews and that is the correct trade — the memo is safe on disk
and extraction is re-derivable (C4). `Proposal` is serialisable by
construction if that ever changes.

## Risk

Moderate, and it is all in one place: the bot's review flow is the only code
that has real confirmation logic today, and it is being rewritten rather than
extended. `tests/test_review.py` (206 lines) is the safety net — it should be
migrated to assert on `Proposal` rather than deleted, since it encodes the
behaviour that must survive.

`tests/test_pipeline.py` and `tests/test_extract.py` should pass untouched. If
they do not, `extract_memo` was not re-expressed faithfully.

## Verification

```
just test          # 96 tests, expected green
just lint
just extract-dry-run
```

Plus one manual pass through the bot: voice note → review message →
approve → check the topic page. The review flow is the part with no
end-to-end coverage.

## Order

Do this before the transport split and well before subtopics. It is the only
one of the three with an actual bug behind it, and it is self-contained enough
to land in a single sitting.

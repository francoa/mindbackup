# Bot confirmation parity

Status: proposed
Date: 2026-09-18
Follows: `2026-09-17_confirmation-stage.md`

## The problem

The confirmation stage landed as a domain object: `Proposal` has
`approve(i)`, `reject(i)` and `reassign(i, topics)`, and
`mindbackup extract --interactive` drives them atom by atom
(`__main__.py:_confirm_interactively`). The bot never calls them.

| | CLI `--interactive` | Bot |
|---|---|---|
| approve one atom | `a` | — |
| reject one atom | `r` | — |
| choose topics | `t` | — |
| approve everything | batch mode (`approve_all`) | ✅ calls `approve_confident` |
| discard everything | — | 🗑 |
| edit | — | ✏️ says "isn't built yet" and does nothing |

Also found while tracing the flow:

- **Unclear atoms are silently dropped.** ✅ runs `approve_confident()`,
  then `commit()`, then removes the proposal from `bot_data`
  (`bot.py:453-460`). The unclear atoms were left pending, but once the
  proposal is removed nothing holds them. The test
  `test_approving_files_the_confident_atoms_and_holds_the_rest` only checks
  that they weren't filed, not that they're still held.
- **The review message asks for a reply that nothing reads.**
  `render_review()` ends with "⚠️ N unclear — reply to tell me what you
  meant" (`review.py:58`). A text reply goes to `handle_other`, which answers
  "Send me a voice note".
- **Dead code.** `CB_RESOLVE` is never used. `render_review_plain()` exists
  only for the Edit branch.
- **Atoms past the twelfth can't be reached.** `MAX_ATOMS_SHOWN = 12` hides
  them, so no control could ever act on them.

## Decisions

1. **Approve everything first, review one by one only if I choose to.** The
   review message offers three buttons: approve all, review one by one,
   discard. Reviewing shows one atom at a time, like the CLI.
2. **Commit once, at the end.** Button taps only change the `Proposal`.
   `commit()` runs once, when the user approves the rest or the last pending
   atom is decided. Leaving the flow partway through never files half a
   review.
3. **New topics come from a text reply.** The topic picker lists known
   topics and also has a "new topic" option. That option asks for a reply,
   and the reply goes to `proposal.reassign`.

## The flow

```
voice note
  └─ review message (overview)
       [✅ Approve all]  [🔍 Review one by one]  [🗑 Discard]
          │                   │                        └─ nothing filed
          │                   └─ one pending atom at a time
          │                        [✅ Keep] [🗑 Drop] [🏷 Topic] [↩️ Back]
          │                          │        │        │          └─ overview, decisions kept
          │                          │        │        └─ topic picker
          │                          │        │             [known topics…] [◀️ ▶️]
          │                          │        │             [✍️ New topic] [↩️ Back]
          │                          │        │                   └─ ForceReply → reassign
          │                          └────────┴─ next pending atom, or commit when none left
          └─ approve_all() on what is still pending → commit() → "Filed N to …"
```

**"As they are" means `approve_all()`.** It files unclear atoms with the
topics the model gave them, the same as the batch CLI. It no longer drops
them. The overview marks unclear atoms with ⚠️ so you can see what you're
approving. It's one tap to review them instead.

`approve_all()` only touches atoms that are still pending. So *Back → Approve
all* keeps the decisions already made during review, and approves the rest.

This leaves `Proposal.approve_confident()` with no caller. Delete it, and
update the "As built" section of the confirmation-stage note, which
describes it as the bot's policy.

## Tasks

### 1. Remove the Edit button

- `review.py`: remove `CB_EDIT`, the ✏️ button and the `__all__` entry.
  Remove `CB_RESOLVE` (the picker below uses its own prefixes).
- `bot.py`: remove the `CB_EDIT` branch, `render_review_plain()` and the
  import.
- `docs/2026-09-17_transport-domain-split.md:25`: update the constants row.

### 2. Overview message

- `render_review` takes the `Proposal`, not the `Extraction`, and marks each
  atom by its state: • pending, ⚠️ pending and unclear, ✅ approved,
  🗑 rejected, 🏷 reassigned (showing the new topics).
- Replace the "reply to tell me what you meant" line with
  "⚠️ N unclear — tap 🔍 to sort them out".
- `review_keyboard(proposal)`: `✅ Approve all (N)`, `🔍 Review one by one`,
  `🗑 Discard`. N is `len(proposal.pending())` plus atoms already approved.
- ✅: `approve_all()` → `commit()` → `render_filed`, then remove the review
  from `bot_data`. Nothing is left pending, so nothing is lost. This fixes the
  dropped-atoms bug.

### 3. One-by-one review

- New `render_atom_step(proposal, index)`: the atom text, its topics, its
  ambiguity question, and "3 of 7".
- Callback data carries the index: `mb:a:<i>` keep, `mb:r:<i>` drop,
  `mb:t:<i>` open the topic picker, `mb:o` back to the overview, `mb:w` start
  reviewing. The `^mb:` pattern already sends all of these to
  `handle_review_button`.
- Split `handle_review_button` by prefix. Each branch calls one `Proposal`
  method, then shows the next pending atom. When no atom is left pending,
  commit and show `render_filed`. This is the same loop as
  `_confirm_interactively`, with no filing logic in the bot.
- Walking through `pending()` also reaches atoms past `MAX_ATOMS_SHOWN`. The
  cap now only applies to the overview.
- An index that is out of range, or already decided (from a double tap),
  should answer the query with a toast and do nothing else. It must not let
  `_decide` raise `IndexError`.

### 4. Topic picker

- Keyboard over `known_topics(settings)`, paginated. Reuse `topic_token()`,
  `resolve_topic()` and `page_slice()` from `browse.py`. Callback data:
  `mb:ts:<i>:<token>` to select a topic, `mb:tp:<i>:<page>` to change page.
- Telegram's 64-byte callback limit: `MAX_TOKEN_BYTES = 58` assumes the
  6-byte `mbt:t:` prefix, but `mb:ts:12:` is 9 bytes. Make `topic_token()`
  take a byte budget instead of reading a module constant.
- Selecting a topic calls `proposal.reassign(i, [topic])` and moves on to the
  next atom. Choosing several topics from buttons is out of scope, because
  the text reply covers it.

### 5. New topics by text reply

- `✍️ New topic` sends a message with `ForceReply`, e.g. "Topics for #3,
  comma-separated:", and stores `prompt_message_id → (review_message_id, i)`
  in `bot_data["topic_prompts"]`.
- New handler: `MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND,
  handle_topic_reply)`, registered **before** `handle_other`. If
  `reply_to_message` isn't a known prompt, it gives the same answer as
  `handle_other` today.
- Parse the reply with a helper shared with the CLI. `_ask_topics` splits on
  commas inline today, so move that into one function both frontends import.
  `reassign` already normalises and deduplicates.
- After reassigning, edit the *review* message to the next atom (or commit),
  and remove the prompt entry. An empty reply re-asks rather than
  reassigning to no topics.
- Topics you typed are rendered with `parse_mode="Markdown"`, so pass them
  through `_escape()`.

### 6. Tests

- `tests/test_review.py`: extend `_run_review` to take a sequence of
  callback data (and optional text replies) instead of a single button.
- Keyboard tests: they currently monkeypatch `review_keyboard` to `None`. Add
  tests that check callback data for every button and every picker page is
  64 bytes or less, including a topic whose slug is over the byte budget.
- Cases:
  - ✅ on the overview files every atom, unclear ones included, and nothing
    is left in `bot_data`.
  - Review → drop one, keep the rest: only the kept atoms reach the vault.
  - Review → 🏷 → pick a known topic: filed under that topic, not the model's.
  - Review → 🏷 → ✍️ → text reply `"gym, health"`: filed under both.
  - Review one atom → Back → ✅: the earlier decision is kept, the rest are
    approved, and there is one commit.
  - Nothing is written until the last decision.
  - A stale index, a double tap, or a reply to an unknown prompt: no
    exception, and a reply is sent.
- Rewrite `test_approving_files_the_confident_atoms_and_holds_the_rest` for
  the new ✅ meaning.
- `tests/test_proposal.py`: only the `approve_confident` tests go. Any other
  change there means logic has moved into the bot.

### 7. Docs and verification

- The confirmation-stage note: in "As built", remove `approve_confident` and
  point to this note.
- README bot section: describe the three buttons and the review flow.
- `just test`, `just lint`.
- Manual pass in the bot: voice note → ✅ on one memo; on another, 🔍 → drop
  one, pick a known topic for one, reply with a new topic for one → check
  the topic pages.

## Order

Task 1 goes first on its own, since it only removes code. Tasks 2–5 go in
order, because each builds on the previous callback prefixes. Tests go in
with each task, not at the end.

## Out of scope

- Editing an atom's *text* in Telegram. Obsidian is the editor.
- Keeping pending reviews across a bot restart. That is still the right
  trade-off (C4), and the "review expired" message still covers it.
- The transport split (`2026-09-17_transport-domain-split.md`). The new
  rendering goes in `review.py` for now and moves together with the rest of
  it.

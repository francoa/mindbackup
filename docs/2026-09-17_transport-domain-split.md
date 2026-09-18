# Separating transport from domain

Status: proposed
Date: 2026-09-17

## The problem

`review.py` and `browse.py` both open with the same justification: *split from
`bot.py` so the handler stays thin and the logic is testable without a Telegram
server.* That was the right move, and it worked — `test_browse.py` and
`test_review.py` exist because of it.

But the split was made along a **testability** line, not an **ownership** line.
The result is two modules sitting in the domain package that are, in fact,
Telegram modules — with genuinely reusable domain logic mixed into them.

What is Telegram-specific in `mindbackup/`:

| Location | Detail |
|---|---|
| `browse.py:26-36` | `MAX_MESSAGE_CHARS = 3500` (Telegram's 4096 limit), `MAX_TOKEN_BYTES = 58` (callback_data limit), `CB_TOPIC` / `CB_PAGE` / `CB_LIST` |
| `browse.py:99-129` | `topics_keyboard()`, `back_keyboard()` — `InlineKeyboardMarkup` |
| `browse.py:42-52` | `topic_token()` — exists only because callback_data is capped at 64 bytes |
| `browse.py:167-199` | `chunk_message()` — splitting for Telegram's message size |
| `review.py:25-27` | `CB_APPROVE` / `CB_DISCARD` / `CB_REVIEW` |
| `review.py:30-32` | `_escape()` — Telegram Markdown escaping |
| `review.py:35-91` | `render_review()`, `review_keyboard()` |

What is genuinely domain, in the same files:

| Location | Detail |
|---|---|
| `browse.py:55-67` | `resolve_topic()` — name or token back to a known topic |
| `browse.py:132-164` | `topic_body()`, `_from_index()`, `_strip_page_furniture()` — "everything filed under this topic, as text" |
| `browse.py:70-81` | `page_count()`, `page_slice()` — generic pagination |
| `review.py:103-120` | `apply_review()` — files approved atoms |

The concrete cost: **an interactive CLI wants `topic_body()` and
`resolve_topic()`, and cannot have them without importing a module whose top
half is `InlineKeyboardButton` and a 3500-character limit that means nothing in
a terminal.** The Telegram imports are lazy (inside functions), so it would
technically work — but the dependency direction would be wrong, and the next
person to touch `browse.py` has to hold two audiences in their head.

`bot_module/` already exists (`authoriser.py`, `utils.py`) as a beachhead for
exactly this separation. It was started and then not finished.

## The target shape

```
src/mindbackup/
  config.py        stt.py       vault.py          — unchanged
  extract.py       llm.py       topics.py         — unchanged
  pipeline.py      proposal.py                    — domain, transport-free
  topic_view.py                                   — NEW: read side of a topic
  telegram/
    __init__.py
    bot.py                                        — moved from bot.py
    authoriser.py  utils.py                       — moved from bot_module/
    render.py                                     — Telegram formatting
    keyboards.py                                  — markup + callback data
    chunking.py                                   — message splitting
  cli/
    __init__.py
    __main__.py                                   — moved from __main__.py
    render.py                                     — terminal formatting
```

The rule: **nothing under `telegram/` or `cli/` may be imported by the domain
modules.** Dependencies point inward only. That is the whole discipline; no
interfaces, no registry, no abstract base classes.

## The moves

**`browse.py` splits three ways.**

- `resolve_topic`, `topic_body`, `_from_index`, `_strip_page_furniture`,
  `page_count`, `page_slice` → `topic_view.py` (domain). This is "read a
  topic", which every frontend needs.
- `topic_token`, `topics_keyboard`, `back_keyboard`, `render_topic_list`,
  `CB_*` → `telegram/keyboards.py` + `telegram/render.py`.
- `chunk_message`, `MAX_MESSAGE_CHARS` → `telegram/chunking.py`.

One wrinkle: `resolve_topic()` currently accepts a Telegram callback *token*
as well as a human-typed name, and matches via `topic_token()`. Domain code
should not know what a token is. Split it:

- `topic_view.resolve_topic(name, topics)` — name or slug, domain.
- `telegram/keyboards.resolve_token(token, topics)` — token first, then
  delegates to the domain function.

**`review.py` splits two ways.**

- `apply_review` → absorbed by `Proposal.commit()` (see the confirmation-stage
  note; do that one first).
- `render_review`, `render_filed`, `review_keyboard`, `_escape`, `CB_*` →
  `telegram/render.py` + `telegram/keyboards.py`.

`review.py` disappears entirely.

**`bot.py` → `telegram/bot.py`**, `bot_module/*` → `telegram/*`.
`bot_module/` is deleted; it was a placeholder for this.

**`__main__.py` → `cli/__main__.py`**, with the `out()` / `err()` / `OK` /
`BAD` / `WARN` / `_render_atom` presentation helpers pulled into
`cli/render.py`. The `cmd_*` functions stay where they are for now — they are
thin, and moving them is a separate argument.

## Entry points to check

`pyproject.toml` declares a console script pointing at `mindbackup.__main__`,
and `Dockerfile` / `docker-compose.yml` / `justfile` all invoke `mindbackup
<command>`. Keep `src/mindbackup/__main__.py` as a two-line shim
(`from mindbackup.cli.__main__ import main`) so `python -m mindbackup` and
every `just` recipe keep working. If the console script is changed instead,
`just doctor` / `just extract` / `just ask` must be re-run against the built
image, not just locally.

## What this is not

Not an abstraction layer over frontends. There is no `Frontend` protocol, no
`Renderer` interface, no registry that a new frontend registers itself with.
Two frontends and one user do not justify any of it, and the moment a third
appears the right move is still probably a third directory, not a framework.

This is a **file move with a dependency rule**. The functions keep their
bodies. That is why it is cheap and why it should be done before the codebase
grows a third consumer, not after.

## Risk

Low. Mostly mechanical, and the test suite is the proof: `test_browse.py` and
`test_review.py` import from the modules being split, so the imports change
but the assertions should not. If an assertion has to change, something moved
to the wrong side of the line — that is the signal to look at, not to patch
the test.

The one judgement call is `resolve_topic`, which is genuinely being split into
two functions rather than moved. Its tests should end up in both places.

## Verification

```
just test           # 96 tests; import paths change, assertions should not
just lint
just doctor         # exercises the console entry point through Docker
just extract-dry-run
python -m mindbackup --help
```

Then start the bot once and run `/get_topic` — browse is the flow with the
most surface area being moved.

## Order

After the confirmation stage, not before. That change deletes `review.py`'s
domain half and rewrites its Telegram half; doing the move first means moving
code that is about to be rewritten.

Independent of the subtopics question, which is a data-model change and
touches none of this.

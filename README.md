# voice-mind-backup

Send a voice note over Telegram to a bot → a transcript
lands in your Obsidian vault, searchable, in under a minute.

Built to a private spec in two milestones. **Milestone 1** is the guarantee:
audio in, raw transcript in the vault, never lost. **Milestone 2** adds an
optional LLM layer on top — summarising, referent resolution and topic pages —
which is off by default and can never cost you a transcript, because it only
runs after the memo is already on disk.

## Pre-requirements:

- Docker / Docker Compose (preferred mode of execution)
- Just (https://github.com/casey/just). Install it via uv/pip: `pip install rust-just`
- Obsidian (https://obsidian.md/). Create a vault to save your memos in


## Quick start (Docker)

Runs the bot in an isolated container — useful because it handles untrusted
input (audio from the internet) while holding a bot token and write access to
your notes.

1. First, create your `.env` file
```bash
cp .env.example .env      # required: compose reads it for secrets AND ${VAR}
echo "UID=$(id -u)"  >> .env
echo "GID=$(id -g)"  >> .env
echo "VAULT_PATH=$HOME/obsidian" >> .env  # Modify with your obsidian vault
echo "ARCHIVE_PATH=$HOME/archive" >> .env
```
2. Create your Telegram Bot by contacting `@BotFather`. Make sure you use the correct capitalization.
3. Fill in your LLM API Token and the Bot Token
```bash
# If you want to prevent the .env from containing secrets that might be read by an Agent, run
echo $BOT_TOKEN > ~/.local/share/voice-mind-backup/token.txt
echo $LLM_API_KEY > ~/.local/share/voice-mind-backup/llm-api-key.txt

# Otherwise, fill the following variables in your .env
echo "MINDBACKUP_TELEGRAM_TOKEN=$BOT_TOKEN" >> .env
echo "MINDBACKUP_LLM_API_KEY=$LLM_API_KEY" >> .env
```
4. Extract your Telegram User ID by contacting `@userinfobot` and set it in the .env
```bash
echo "MINDBACKUP_ALLOWED_USERS=$USER_ID" >> .env
```
5. Finally, run
```bash
just start
```

## What it does

```
phone voice note via telegram──> bot ──whisper──> <vault>/Memos/2026-09-04.md
                                  └──copy────> <archive>/2026-09/20260904T134922-voice.ogg
```

Each memo is exactly this, nothing more:

```markdown
---
date: 2026-09-04
type: memo
source: telegram
---

Physio session today. She said my lower back pain comes from tight hamstrings...
```

Filenames are `<YYYY-MM-DD>.md`, with `_2`, `_3` suffixes for later memos the
same day. The raw transcript layer is immutable and complete (constraint C4):
memos are never overwritten, and the source audio is archived so every
transcript can be re-derived later with a better model.

## Running locally

```bash
git clone https://github.com/<you>/voice-mind-backup.git
cd voice-mind-backup
uv sync --extra local --extra dev   # exact versions from uv.lock
. .venv/bin/activate
cp .env.example .env                 # then edit it
mindbackup doctor
```

Dependencies are locked in `uv.lock` (committed, sha256-pinned) and resolved
under a 7-day supply-chain cooling-off window — artifacts uploaded in the last
week are ignored, so a compromised release has time to be caught and yanked
before it reaches this machine. `uv sync --locked` fails rather than silently
re-resolving if the lock and `pyproject.toml` disagree. Refresh deliberately
with `uv lock --upgrade`.

`doctor` checks every precondition — vault writable, STT installed, token
present — and exits non-zero if something would block ingest. Run it first, and
run it again whenever something feels broken.

### Run

```bash
mindbackup bot                          # long-polling Telegram bot
mindbackup ingest path/to/audio.ogg     # same pipeline, no Telegram
mindbackup doctor                       # preflight checks
```

`ingest` runs the *identical* code path the bot does, so if it works here it
works from the phone.

## Docker Design Notes

- Multi-stage: deps resolve from `uv.lock` in the builder, only the finished
  venv reaches the runtime image.
- The Whisper model is downloaded at **build** time into `HF_HUB_CACHE`, and
  `HF_HUB_OFFLINE=1` at runtime — the container never calls Hugging Face while
  running, and a first memo isn't slowed by a model download.
  (`HF_HUB_CACHE`, not `HF_HOME` — `download_model(cache_dir=X)` writes to
  `X/models--…` whereas `HF_HOME=X` looks in `X/hub`, so the offline load
  fails if you use the wrong one.)
- `read_only: true` with a tmpfs `/tmp`: voice notes are downloaded, transcribed
  and discarded in RAM. Nothing outside the vault and archive mounts persists.
- `cap_drop: ALL` + `no-new-privileges`, non-root uid, and **no inbound ports**
  — long polling means nothing needs to be exposed.
- Change the model without touching code: `WHISPER_MODEL=small docker compose build`.

## Transcription accuracy

Default is local `faster-whisper` (`medium`) — private, no API key, ~8 s for a
30 s memo, ~2.6 GB peak RAM. Chosen by benchmarking every model size against a
real 81 s Spanish recording; full method, numbers and transcripts in
[`tests/BENCHMARK.md`](tests/BENCHMARK.md).

| model | disk | peak RSS | WER | **key-term recall** | ~30 s memo |
|---|---|---|---|---|---|
| tiny | 75 M | — | 0.393 | 0.54 | ~0.6 s |
| base | 142 M | 0.4 G | 0.274 | 0.69 | ~1.2 s |
| small | 464 M | 0.9 G | **0.119** | 0.85 | ~4.8 s |
| **medium** | 1.5 G | 2.6 G | 0.131 | **0.92** | ~7.8 s |
| large-v3 | 2.9 G | 4.6 G | 0.125 | 0.85 | ~12.3 s |

`small` has the best word error rate but `medium` is the better *memo*: WER
punishes `medium`'s comma-vs-period style, while `small`'s errors destroy
content — "milanesas y papas fritas" became "miran estas y papas pericas", and
"cyber seguridad" became "el server seguridad". A memo you can't find is worse
than a memo with odd commas.

`large-v3` is not an upgrade: same recall as `small`, 1.5× slower than
`medium`, 4.6 GB peak, and it alone dropped "hacking" and hallucinated
"Leisure Vampire" for "Age of Empires". Bigger is not monotonically better on
noisy 8 kHz phone audio.

Re-run the benchmark whenever you add recordings:

```bash
python tests/benchmark_models.py tiny base small medium large-v3 --language es
```

**If `medium` is too slow or too big**, set `MINDBACKUP_STT_MODEL=small` (and
rebuild the image — the model is baked in). Check
`deploy.resources.limits.memory` in `docker-compose.yml` covers the model's
peak RSS; a container over its limit is SIGKILLed mid-transcription, and with
`restart: unless-stopped` that becomes a crash loop. `test_benchmark.py`
enforces this invariant.

**Jargon is the second accuracy lever**, worth roughly one model size for free.
Term recall with vs. without `MINDBACKUP_VOCABULARY`: tiny 0.39→0.54, base
0.54→0.69, small 0.69→0.85, medium 0.85→0.92. Two rules, both learned the hard
way on real audio:

1. **Keep it a bare comma-separated word list.** A labelled form like
   `Padel terms: ...` leaks into the transcript ("Quick memo after Padel terms
   today").
2. **It must end in a period** — the code appends one for you. Whisper
   continues the *style* of its prompt, so an unpunctuated hint produces an
   unpunctuated transcript: *"physio session today She said my lower back
   pain…"* instead of *"Physio session today. She said my lower back pain…"*.

Add words whenever you notice a miss; it costs nothing.

Leave `MINDBACKUP_STT_LANGUAGE` empty if you code-switch between Spanish and
English — forcing a language degrades the other one.


## Tests

```bash
python -m pytest -q
```

Covers title generation, filename safety, collision handling, frontmatter
shape, timezone-correct dating, and allowlist parsing. No network, no model
download.
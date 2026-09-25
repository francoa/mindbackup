#!/usr/bin/env bash
# Interactive first-time setup: walks through every setting the bot needs and
# writes .env plus the Docker secret files.
#
# Re-runnable: every prompt defaults to what is already configured, so pressing
# Enter keeps it. Re-run a single step with `--step <name>` (see `--list`).
#
# To add a step: write a `step_<name>` function and add a line to STEPS.

set -euo pipefail

ROOT="${MINDBACKUP_SETUP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="$ROOT/.env"
ENV_TEMPLATE="$ROOT/.env.example"
# Fixed by docker-compose.yml's `secrets:` block; overridable only for tests.
SECRETS_DIR="${MINDBACKUP_SECRETS_DIR:-$HOME/.local/share/voice-mind-backup}"
BOT_TOKEN_FILE="$SECRETS_DIR/token.txt"
LLM_KEY_FILE="$SECRETS_DIR/llm-api-key.txt"
BIN_DIR="$ROOT/bin"

# name|title — order is the order they run in.
STEPS=(
  "prereqs|Pre-requirements"
  "env|Base .env file"
  "telegram|Telegram bot"
  "users|Allowed Telegram users"
  "vault|Obsidian vault and archive"
  "llm|LLM (optional)"
  "stt|Transcription"
  "timezone|Timezone"
  "video|Video links (optional)"
  "finish|Summary, build and check"
)

OFFLINE=0
BOT_TOKEN=""

# --- Output -----------------------------------------------------------------

if ((BASH_VERSINFO[0] < 4)); then
  echo "This script needs bash 4 or newer (macOS: brew install bash)." >&2
  exit 1
fi

if [[ -t 2 ]]; then
  BOLD=$'\e[1m' DIM=$'\e[2m' RED=$'\e[31m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RESET=$'\e[0m'
else
  BOLD="" DIM="" RED="" GREEN="" YELLOW="" RESET=""
fi

# Everything shown to the user goes to stderr: stdout carries the values that
# helpers return through $(...).
say() { printf '%s\n' "$*" >&2; }
info() { printf '%s\n' "${DIM}$*${RESET}" >&2; }
ok() { printf '%s\n' "${GREEN}✓${RESET} $*" >&2; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*" >&2; }
fail() { printf '%s\n' "${RED}✗${RESET} $*" >&2; }
die() { fail "$*"; exit 1; }

# --- Prompts ----------------------------------------------------------------
# All prompts read stdin, so the script can be driven by piped answers.

# ask "Question" default -> answer (default when Enter is pressed)
ask() {
  local question=$1 default=${2:-} answer
  if [[ -n $default ]]; then
    printf '%s [%s]: ' "$question" "$default" >&2
  else
    printf '%s: ' "$question" >&2
  fi
  IFS= read -r answer || die "No more input."
  printf '%s' "${answer:-$default}"
}

# ask_secret "Question" has_existing -> answer, typed without echo. Empty means
# "keep the existing one" when has_existing is 1.
ask_secret() {
  local question=$1 has_existing=${2:-0} answer
  if [[ $has_existing == 1 ]]; then
    printf '%s [Enter keeps the current one]: ' "$question" >&2
  else
    printf '%s: ' "$question" >&2
  fi
  IFS= read -rs answer || die "No more input."
  printf '\n' >&2
  printf '%s' "$answer"
}

# confirm "Question" y|n -> exit status
confirm() {
  local question=$1 default=${2:-y} hint answer
  [[ $default == y ]] && hint="Y/n" || hint="y/N"
  while true; do
    printf '%s [%s]: ' "$question" "$hint" >&2
    IFS= read -r answer || die "No more input."
    answer=${answer:-$default}
    case ${answer,,} in
      y | yes) return 0 ;;
      n | no) return 1 ;;
    esac
  done
}

pause() {
  printf '%s' "${DIM}Press Enter to continue...${RESET}" >&2
  IFS= read -r _ || die "No more input."
}

mask() {
  local value=$1
  if [[ -z $value ]]; then
    printf '(not set)'
  elif ((${#value} <= 8)); then
    printf '••••••'
  else
    printf '%s••••••%s' "${value:0:4}" "${value: -3}"
  fi
}

# --- .env editing -----------------------------------------------------------

env_get() {
  [[ -f $ENV_FILE ]] || return 0
  local line
  line=$(grep -E "^$1=" "$ENV_FILE" | tail -n 1) || return 0
  line=${line#*=}
  # Same quote stripping as config.load_env_file.
  if ((${#line} >= 2)) && [[ ${line:0:1} == "${line: -1}" && ${line:0:1} == [\"\'] ]]; then
    line=${line:1:${#line}-2}
  fi
  printf '%s' "$line"
}

_ENV_BACKED_UP=0
_backup_env() {
  if [[ $_ENV_BACKED_UP == 0 && -f $ENV_FILE ]]; then
    cp -p "$ENV_FILE" "$ENV_FILE.bak"
    _ENV_BACKED_UP=1
  fi
}

# Replace KEY in place (dropping duplicates), or append it.
env_set() {
  local key=$1 value=$2
  _backup_env
  touch "$ENV_FILE"
  local tmp
  tmp=$(mktemp "$ENV_FILE.XXXXXX")
  KEY="$key" VALUE="$value" awk '
    BEGIN { key = ENVIRON["KEY"]; value = ENVIRON["VALUE"]; done = 0 }
    index($0, key "=") == 1 { if (!done) { print key "=" value; done = 1 } next }
    { print }
    END { if (!done) print key "=" value }
  ' "$ENV_FILE" >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

env_unset() {
  local key=$1
  [[ -f $ENV_FILE ]] && grep -qE "^$key=" "$ENV_FILE" || return 0
  _backup_env
  local tmp
  tmp=$(mktemp "$ENV_FILE.XXXXXX")
  KEY="$key" awk 'index($0, ENVIRON["KEY"] "=") != 1' "$ENV_FILE" >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

# --- Secrets ----------------------------------------------------------------

write_secret() {
  local file=$1 value=$2
  mkdir -p "$SECRETS_DIR"
  chmod 700 "$SECRETS_DIR"
  (umask 077 && printf '%s\n' "$value" >"$file")
  chmod 600 "$file"
}

read_secret_file() {
  [[ -f $1 ]] && tr -d '[:space:]' <"$1" || true
}

# Compose refuses to start if a declared secret file is missing, so both must
# exist even when empty. An empty file means "fall back to .env".
ensure_secret_file() {
  [[ -f $1 ]] && return 0
  write_secret "$1" ""
}

# Where a secret lives today: the secret file wins, like config._get_from_secrets.
current_secret() {
  local file=$1 env_key=$2 value
  value=$(read_secret_file "$file")
  [[ -n $value ]] || value=$(env_get "$env_key")
  printf '%s' "$value"
}

# store_secret FILE ENV_KEY VALUE — ask where to keep it, then write it there.
store_secret() {
  local file=$1 env_key=$2 value=$3
  say ""
  say "Where should it be stored?"
  say "  1) Secret file ${DIM}$file${RESET} (recommended: keeps it out of .env,"
  say "     which coding agents and editors read freely)"
  say "  2) .env as $env_key"
  local choice
  choice=$(ask "Choice" "1")
  if [[ $choice == 2 ]]; then
    env_set "$env_key" "$value"
    write_secret "$file" ""
    ok "Saved in .env."
  else
    write_secret "$file" "$value"
    env_unset "$env_key"
    ok "Saved in $file (mode 600)."
  fi
}

# --- HTTP -------------------------------------------------------------------
# Secrets go through curl's config on stdin, not argv, so they never show up in
# the process list.

telegram_api() {
  local method=$1
  printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$BOT_TOKEN" "$method" |
    curl -sS --max-time 15 -K -
}

load_bot_token() {
  [[ -n $BOT_TOKEN ]] || BOT_TOKEN=$(current_secret "$BOT_TOKEN_FILE" MINDBACKUP_TELEGRAM_TOKEN)
}

# String values of "field" in a JSON response, one per line.
json_strings() {
  grep -oE "\"$1\": *\"[^\"]*\"" | sed -E 's/.*"([^"]*)"$/\1/' || true
}

json_field() { json_strings "$1" | head -n 1; }

have() { command -v "$1" >/dev/null 2>&1; }

compose() {
  if have docker-compose; then
    docker-compose "$@"
  else
    docker compose "$@"
  fi
}

# Expand ~ and make relative paths relative to the repo, like compose does.
resolve_path() {
  local path=${1/#\~/$HOME}
  [[ $path == /* ]] || path="$ROOT/${path#./}"
  printf '%s' "$path"
}

# --- Steps ------------------------------------------------------------------

step_prereqs() {
  local missing=0
  if have docker; then ok "docker"; else fail "docker — https://docs.docker.com/engine/install/"; missing=1; fi
  if have docker-compose || (have docker && docker compose version >/dev/null 2>&1); then
    ok "docker compose"
  else
    fail "docker compose — https://docs.docker.com/compose/install/"
    missing=1
  fi
  if have docker && ! docker info >/dev/null 2>&1; then
    warn "Docker is installed but not reachable. Start the daemon, and make sure"
    warn "your user can use it without sudo: https://docs.docker.com/engine/install/linux-postinstall/"
    missing=1
  fi
  if have just; then
    ok "just"
  elif have make; then
    ok "make (just is not installed; use \`make <target>\` instead of \`just <target>\`)"
  else
    fail "just — pip install rust-just (or https://github.com/casey/just)"
    missing=1
  fi
  if have curl; then ok "curl"; else fail "curl — install it with your package manager"; missing=1; fi

  say ""
  info "Not checked: Obsidian (https://obsidian.md). You need it to read your notes,"
  info "but the bot only needs the vault folder, which step 5 creates."

  if [[ $missing == 1 ]]; then
    say ""
    confirm "Some requirements are missing. Continue anyway?" n || exit 1
  fi
}

step_env() {
  if [[ -f $ENV_FILE ]]; then
    ok ".env already exists; its values are the defaults from here on."
  elif [[ -f $ENV_TEMPLATE ]]; then
    cp "$ENV_TEMPLATE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "Created .env from .env.example."
  else
    (umask 077 && touch "$ENV_FILE")
    ok "Created an empty .env."
  fi

  # The container runs as this uid so it can write the vault on the host.
  env_set UID "$(id -u)"
  env_set GID "$(id -g)"
  ok "UID=$(id -u) GID=$(id -g) (the container writes your vault as you)."
}

step_telegram() {
  say "The bot needs its own Telegram account. To create one:"
  say "  1. In Telegram, open ${BOLD}@BotFather${RESET} (exact spelling, blue check mark)."
  say "  2. Send /newbot."
  say "  3. Pick a display name, then a username ending in \"bot\" (e.g. my_memos_bot)."
  say "  4. BotFather replies with a token like 123456789:AAH...; copy it."
  say ""

  local existing token
  existing=$(current_secret "$BOT_TOKEN_FILE" MINDBACKUP_TELEGRAM_TOKEN)
  [[ -n $existing ]] && info "Current token: $(mask "$existing")"

  while true; do
    token=$(ask_secret "Bot token" "$([[ -n $existing ]] && echo 1 || echo 0)")
    token=${token:-$existing}
    if [[ ! $token =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
      warn "That doesn't look like a bot token (digits, a colon, then letters)."
      continue
    fi
    BOT_TOKEN=$token
    if [[ $OFFLINE == 1 ]]; then
      info "Offline: not checking the token with Telegram."
      break
    fi
    local response
    response=$(telegram_api getMe || true)
    if [[ $response == *'"ok":true'* ]]; then
      ok "Token works for @$(json_field username <<<"$response")."
      break
    fi
    fail "Telegram rejected the token: $(json_field description <<<"$response")"
    confirm "Try another token?" y || break
  done

  store_secret "$BOT_TOKEN_FILE" MINDBACKUP_TELEGRAM_TOKEN "$BOT_TOKEN"
}

# Print "id<TAB>name" per distinct sender in a getUpdates response.
_senders() {
  if have jq; then
    jq -r '[.result[] | (.message // .edited_message // .callback_query // empty) | .from]
           | unique_by(.id)[]
           | "\(.id)\t\([.first_name, .last_name] | map(select(.)) | join(" "))\(if .username then " (@" + .username + ")" else "" end)"'
  elif have python3; then
    python3 -c '
import json, sys
seen = {}
for update in json.load(sys.stdin).get("result", []):
    for kind in ("message", "edited_message", "callback_query"):
        sender = (update.get(kind) or {}).get("from")
        if sender:
            name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
            if sender.get("username"):
                name += " (@" + sender["username"] + ")"
            seen[sender["id"]] = name
for uid, name in seen.items():
    print(f"{uid}\t{name}")
'
  else
    return 1
  fi
}

_users_from_updates() {
  load_bot_token
  if [[ -z $BOT_TOKEN ]]; then
    warn "No bot token yet (run the telegram step first)."
    return 1
  fi
  if [[ $OFFLINE == 1 ]]; then
    warn "Offline: can't ask Telegram."
    return 1
  fi
  if ! have jq && ! have python3; then
    warn "Needs jq or python3 to read Telegram's reply."
    return 1
  fi

  say "Stop the bot if it is running (\`just stop\`): Telegram only hands messages"
  say "to one reader at a time."
  say "Now send any message (e.g. \"hi\") to your bot from every account that should"
  say "be allowed to use it."
  pause

  local response
  response=$(telegram_api getUpdates || true)
  if [[ $response != *'"ok":true'* ]]; then
    fail "Telegram said: $(json_field description <<<"$response")"
    return 1
  fi

  local -a ids=() names=()
  local id name
  while IFS=$'\t' read -r id name; do
    [[ -n $id ]] || continue
    ids+=("$id")
    names+=("$name")
  done < <(_senders <<<"$response")

  if ((${#ids[@]} == 0)); then
    warn "No messages found. Send one to the bot and try again."
    return 1
  fi

  say ""
  say "Messages came from:"
  local i
  for i in "${!ids[@]}"; do
    say "  $((i + 1))) ${names[$i]}  ${DIM}id ${ids[$i]}${RESET}"
  done
  local picked selected=()
  picked=$(ask "Which to allow (numbers, comma-separated)" "all")
  if [[ $picked == all ]]; then
    selected=("${ids[@]}")
  else
    local n
    for n in ${picked//,/ }; do
      [[ $n =~ ^[0-9]+$ ]] && ((n >= 1 && n <= ${#ids[@]})) && selected+=("${ids[$((n - 1))]}")
    done
  fi

  # Mark the updates read so the bot doesn't answer these test messages.
  local last_update
  last_update=$(grep -o '"update_id":[0-9]*' <<<"$response" | tail -n 1 | cut -d: -f2)
  if [[ -n $last_update ]]; then
    telegram_api "getUpdates?offset=$((last_update + 1))" >/dev/null || true
  fi

  local IFS=,
  printf '%s' "${selected[*]}"
}

step_users() {
  say "Only these Telegram accounts may use the bot (MINDBACKUP_ALLOWED_USERS);"
  say "it refuses to start with an empty list."
  say ""
  say "  1) Automatic: message your bot, then pick the senders here (recommended)"
  say "  2) Manual: get each numeric id from ${BOLD}@userinfobot${RESET} and type it in"

  local existing found="" proposed
  existing=$(env_get MINDBACKUP_ALLOWED_USERS)
  [[ -n $existing ]] && info "Current: $existing"

  if [[ $(ask "Choice" "1") != 2 ]]; then
    found=$(_users_from_updates) || warn "Falling back to typing the ids in."
  else
    say "Open @userinfobot in Telegram and send it any message; it replies with your Id."
  fi

  # Merge what was already allowed with what was just picked, keeping order.
  proposed=$(printf '%s,%s' "$existing" "$found" | tr -s ', ' '\n' | awk 'NF && !seen[$0]++' | paste -sd, -)

  local users
  while true; do
    users=$(ask "Allowed user ids, comma-separated" "$proposed")
    users=${users// /}
    if [[ $users =~ ^[0-9]+(,[0-9]+)*$ ]]; then
      break
    fi
    warn "Ids are numbers separated by commas, e.g. 123456789,987654321."
  done
  env_set MINDBACKUP_ALLOWED_USERS "$users"
  ok "MINDBACKUP_ALLOWED_USERS=$users"
}

_ask_dir_name() {
  local key=$1 label=$2 default=$3 value
  while true; do
    value=$(ask "$label" "$(env_get "$key" | grep . || echo "$default")")
    if [[ -n $value && $value != /* && $value != *..* ]]; then
      break
    fi
    warn "Use a folder name inside the vault, e.g. $default."
  done
  env_set "$key" "$value"
}

step_vault() {
  say "An Obsidian vault is just a folder of Markdown files. The bot writes into it;"
  say "Obsidian reads it."
  say ""

  local vault resolved
  while true; do
    # shellcheck disable=SC2088  # shown as-is; resolve_path expands it
    vault=$(ask "Vault folder (VAULT_PATH)" "$(env_get VAULT_PATH | grep . || echo "~/obsidian")")
    resolved=$(resolve_path "$vault")
    if [[ ! -d $resolved ]]; then
      confirm "$resolved doesn't exist. Create it?" y || continue
      mkdir -p "$resolved"
    fi
    if [[ -w $resolved ]]; then
      break
    fi
    fail "$resolved is not writable by $(id -un); the container couldn't write memos there."
  done
  env_set VAULT_PATH "$resolved"
  ok "VAULT_PATH=$resolved"

  _ask_dir_name MINDBACKUP_MEMO_DIR "Folder for transcripts, inside the vault" Memos
  _ask_dir_name MINDBACKUP_TOPIC_DIR "Folder for topic pages, inside the vault" Topics

  say ""
  say "The original audio is archived too, so transcripts can be redone later with a"
  say "better model. It can live outside the vault."
  local archive
  archive=$(ask "Audio archive folder (ARCHIVE_PATH)" "$(env_get ARCHIVE_PATH | grep . || echo "./archive")")
  resolved=$(resolve_path "$archive")
  mkdir -p "$resolved"
  env_set ARCHIVE_PATH "$resolved"
  ok "ARCHIVE_PATH=$resolved"

  say ""
  info "In Obsidian: \"Open folder as vault\" and pick $(env_get VAULT_PATH)."
}

step_llm() {
  say "The LLM layer summarises memos and files them into topic pages. It is"
  say "optional: without it, the bot still saves every transcript."
  say ""
  local existing
  existing=$(current_secret "$LLM_KEY_FILE" MINDBACKUP_LLM_API_KEY)
  if ! confirm "Set up the LLM?" y; then
    ensure_secret_file "$LLM_KEY_FILE"
    ok "Skipped. Re-run with \`just setup --step llm\` to enable it later."
    return 0
  fi

  say ""
  info "Any OpenAI-compatible endpoint works. The default is the Claude API; get a"
  info "key at https://console.anthropic.com/settings/keys"
  local base_url
  base_url=$(ask "API base URL" "$(env_get MINDBACKUP_LLM_BASE_URL | grep . || echo "https://api.anthropic.com/v1")")
  base_url=${base_url%/}
  env_set MINDBACKUP_LLM_BASE_URL "$base_url"

  [[ -n $existing ]] && info "Current key: $(mask "$existing")"
  local key models=""
  while true; do
    key=$(ask_secret "API key" "$([[ -n $existing ]] && echo 1 || echo 0)")
    key=${key:-$existing}
    if [[ -z $key ]]; then
      warn "The key can't be empty."
      continue
    fi
    if [[ $OFFLINE == 1 ]]; then
      info "Offline: not checking the key."
      break
    fi
    local response status
    response=$(printf 'url = "%s/models"\nheader = "Authorization: Bearer %s"\nheader = "x-api-key: %s"\nheader = "anthropic-version: 2023-06-01"\n' \
      "$base_url" "$key" "$key" | curl -sS --max-time 15 -w '\n%{http_code}' -K - || true)
    status=${response##*$'\n'}
    if [[ $status == 200 ]]; then
      ok "Key accepted."
      models=$(json_strings id <<<"$response")
      break
    elif [[ $status == 401 || $status == 403 ]]; then
      fail "The provider rejected the key (HTTP $status)."
      confirm "Try another key?" y && continue
      break
    else
      warn "Couldn't verify the key (HTTP ${status:-no response}); saving it anyway."
      break
    fi
  done
  store_secret "$LLM_KEY_FILE" MINDBACKUP_LLM_API_KEY "$key"

  local default_model model
  default_model=$(env_get MINDBACKUP_LLM_MODEL | grep . || echo "claude-haiku-4-5")
  if [[ -n $models ]]; then
    say ""
    say "Models available to this key:"
    local -a list
    mapfile -t list <<<"$models"
    local i
    for i in "${!list[@]}"; do say "  $((i + 1))) ${list[$i]}"; done
    model=$(ask "Model (number or id)" "$default_model")
    if [[ $model =~ ^[0-9]+$ ]] && ((model >= 1 && model <= ${#list[@]})); then
      model=${list[$((model - 1))]}
    fi
  else
    model=$(ask "Model id" "$default_model")
  fi
  env_set MINDBACKUP_LLM_MODEL "$model"
  ok "MINDBACKUP_LLM_MODEL=$model"
}

step_stt() {
  say "Speech-to-text runs locally with Whisper. Bigger models are more accurate but"
  say "slower and need more RAM (measured on real Spanish memos, see tests/BENCHMARK.md):"
  say ""
  say "  model      peak RAM  key-term recall  ~30 s memo"
  say "  tiny       ~0.3 G    0.54             ~0.6 s"
  say "  base       0.4 G     0.69             ~1.2 s"
  say "  small      0.9 G     0.85             ~4.8 s"
  say "  medium     2.6 G     0.92             ~7.8 s   (recommended)"
  say "  large-v3   4.6 G     0.85             ~12.3 s"
  say ""

  local model limit
  while true; do
    model=$(ask "Model" "$(env_get MINDBACKUP_STT_MODEL | grep . || echo medium)")
    case $model in
      tiny | base) limit=1g ;;
      small) limit=2g ;;
      medium) limit=4g ;;
      large-v3) limit=6g ;;
      *) warn "Pick one of: tiny, base, small, medium, large-v3."; continue ;;
    esac
    break
  done
  local previous
  previous=$(env_get MINDBACKUP_STT_MODEL)
  env_set MINDBACKUP_STT_MODEL "$model"
  # Above the model's peak: a container over its limit is killed mid-memo.
  env_set MINDBACKUP_MEMORY_LIMIT "$limit"
  ok "MINDBACKUP_STT_MODEL=$model (container memory limit $limit)"
  if [[ -n $previous && $previous != "$model" ]]; then
    warn "The model is baked into the image: run \`just build\` before starting."
  fi

  say ""
  info "Leave the language empty if you mix languages; forcing one hurts the other."
  local language
  language=$(ask "Language code (e.g. es, en; empty = detect)" "$(env_get MINDBACKUP_STT_LANGUAGE)")
  env_set MINDBACKUP_STT_LANGUAGE "$language"

  say ""
  info "Words Whisper tends to get wrong (names, jargon), as a plain comma-separated"
  info "list: \"Padel, Obsidian, Kubernetes\". No labels like \"Terms: ...\"."
  local vocabulary
  vocabulary=$(ask "Vocabulary (optional)" "$(env_get MINDBACKUP_VOCABULARY)")
  env_set MINDBACKUP_VOCABULARY "$vocabulary"
}

_detect_timezone() {
  local tz=""
  if have timedatectl; then tz=$(timedatectl show -p Timezone --value 2>/dev/null || true); fi
  if [[ -z $tz && -f /etc/timezone ]]; then tz=$(tr -d '[:space:]' </etc/timezone); fi
  if [[ -z $tz && -L /etc/localtime ]]; then tz=$(readlink /etc/localtime | sed 's|.*zoneinfo/||'); fi
  printf '%s' "$tz"
}

step_timezone() {
  local tz
  tz=$(ask "Timezone, used to date memos" "$(env_get MINDBACKUP_TIMEZONE | grep . || _detect_timezone)")
  env_set MINDBACKUP_TIMEZONE "$tz"
  ok "MINDBACKUP_TIMEZONE=$tz"
}

# The README's yt-dlp example. The binary lives in ./bin, mounted at /app/bin.
YTDLP_COMMAND='/app/bin/yt-dlp --skip-download --sleep-subtitles 60 --no-playlist --write-subs --write-auto-subs --sub-langs ".*-orig,es.*" --sub-format vtt -o "{out_dir}/%(id)s.%(ext)s" -- {url}'
YTDLP_URL_PATTERN='(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?\S*v=|shorts/|live/)|youtu\.be/)[\w-]{11}\S*'
DEFAULT_VIDEO_URL_PATTERN='https?://\S+'

_valid_regex() {
  # The bot compiles it with Python's re; without python3, trust it.
  have python3 || return 0
  python3 -c 'import re, sys; re.compile(sys.argv[1])' "$1" 2>/dev/null
}

_video_custom() {
  info "The bot runs it without a shell. {url} is the link (appended if missing);"
  info "the command must write a .vtt or .srt file into {out_dir}."
  local command pattern
  local video_command="$(env_get MINDBACKUP_VIDEO_COMMAND | grep . || echo "$YTDLP_COMMAND")"
  local url_pattern="$(env_get MINDBACKUP_VIDEO_URL_PATTERN | grep . || echo "$YTDLP_URL_PATTERN")"
  while true; do
    command=$(ask "Command" "$video_command")
    [[ -n $command ]] && break
    warn "The command can't be empty (choose \"off\" instead)."
  done
  [[ $command == *"{out_dir}"* ]] ||
    warn "No {out_dir} in the command: it must still write its subtitles there."
  env_set MINDBACKUP_VIDEO_COMMAND "$command"

  info "Only messages matching this regex, as a whole, are treated as video links."
  while true; do
    pattern=$(ask "URL pattern" "$url_pattern")
    _valid_regex "$pattern" && break
    warn "That isn't a valid (Python) regex."
  done
  env_set MINDBACKUP_VIDEO_URL_PATTERN "$pattern"
  ok "Video links are on."
}

step_video() {
  say "Send the bot a video link and it files the video's transcript as a memo, then"
  say "offers it for review like a voice note. It works by running a command that"
  say "fetches the subtitles; the bot has no downloader built in."
  say ""
  say "  1) Off"
  say "  2) On"

  local current default=1
  current=$(env_get MINDBACKUP_VIDEO_COMMAND)
  if [[ $current == "$YTDLP_COMMAND" ]]; then
    default=2
  elif [[ -n $current ]]; then
    default=3
    info "Current command: $current"
  fi

  local choice
  while true; do
    choice=$(ask "Choice" "$default")
    [[ $choice == [12] ]] && break
  done
  case $choice in
    1)
      env_set MINDBACKUP_VIDEO_COMMAND ""
      ok "Video links are off."
      return 0
      ;;
    2) _video_custom ;;
  esac

  local timeout
  while true; do
    timeout=$(ask "Seconds to wait for the command" "$(env_get MINDBACKUP_VIDEO_TIMEOUT | grep . || echo 120)")
    [[ $timeout =~ ^[0-9]+(\.[0-9]+)?$ ]] && break
    warn "A number of seconds, e.g. 120."
  done
  env_set MINDBACKUP_VIDEO_TIMEOUT "$timeout"
}

step_finish() {
  local bot llm
  bot=$(current_secret "$BOT_TOKEN_FILE" MINDBACKUP_TELEGRAM_TOKEN)
  llm=$(current_secret "$LLM_KEY_FILE" MINDBACKUP_LLM_API_KEY)
  # Compose won't start without both secret files, even if they are empty.
  ensure_secret_file "$BOT_TOKEN_FILE"
  ensure_secret_file "$LLM_KEY_FILE"

  say "Configuration:"
  local key
  for key in VAULT_PATH MINDBACKUP_MEMO_DIR MINDBACKUP_TOPIC_DIR ARCHIVE_PATH \
    MINDBACKUP_ALLOWED_USERS MINDBACKUP_STT_MODEL MINDBACKUP_MEMORY_LIMIT \
    MINDBACKUP_STT_LANGUAGE MINDBACKUP_TIMEZONE MINDBACKUP_LLM_BASE_URL MINDBACKUP_LLM_MODEL \
    MINDBACKUP_VIDEO_COMMAND; do
    printf '  %-26s %s\n' "$key" "$(env_get "$key")"
  done
  printf '  %-26s %s\n' "Telegram token" "$(mask "$bot")"
  printf '  %-26s %s\n' "LLM API key" "$(mask "$llm")"
  say ""

  [[ -n $bot ]] || warn "No Telegram token: the bot won't start (run \`just setup --step telegram\`)."
  [[ -n $(env_get MINDBACKUP_ALLOWED_USERS) ]] || warn "No allowed users: the bot won't start (run \`just setup --step users\`)."

  if ! have docker; then
    info "Install Docker, then run \`just build\` and \`just start\`."
    return 0
  fi
  if confirm "Build the Docker image now? (the first build downloads the Whisper model)" y; then
    (cd "$ROOT" && compose build mindbackup) || { fail "Build failed."; return 1; }
    ok "Image built."
    if confirm "Run the preflight check (doctor)?" y; then
      if (cd "$ROOT" && compose run --rm mindbackup doctor); then
        ok "All checks passed."
      else
        warn "Doctor found problems (see above). Fix them and re-run the matching step."
      fi
    fi
  fi
  say ""
  say "${BOLD}Done.${RESET} Start the bot with \`just start\` (or \`make start\`), then send it a voice note."
}

# --- Main -------------------------------------------------------------------

usage() {
  cat <<EOF
Usage: scripts/setup.sh [--step NAME]... [--list] [--offline]

  --step NAME  Run only this step (repeatable). Default: all steps, in order.
  --list       List the steps.
  --offline    Skip every network check and download.
EOF
}

list_steps() {
  local entry i=1
  for entry in "${STEPS[@]}"; do
    printf '  %2d. %-9s %s\n' "$i" "${entry%%|*}" "${entry#*|}"
    ((i++))
  done
}

main() {
  local -a only=()
  while (($#)); do
    case $1 in
      --step) [[ $# -ge 2 ]] || die "--step needs a name"; only+=("$2"); shift 2 ;;
      --step=*) only+=("${1#*=}"); shift ;;
      --list) list_steps; exit 0 ;;
      --offline) OFFLINE=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; exit 2 ;;
    esac
  done

  local -a run=()
  local entry name
  if ((${#only[@]} == 0)); then
    run=("${STEPS[@]}")
  else
    for name in "${only[@]}"; do
      local found=0
      for entry in "${STEPS[@]}"; do
        if [[ ${entry%%|*} == "$name" ]]; then run+=("$entry"); found=1; fi
      done
      [[ $found == 1 ]] || die "Unknown step '$name'. Steps: $(printf '%s ' "${STEPS[@]%%|*}")"
    done
  fi

  local total=${#run[@]} i=1
  for entry in "${run[@]}"; do
    say ""
    say "${BOLD}Step $i/$total — ${entry#*|}${RESET}"
    say ""
    "step_${entry%%|*}"
    ((i++))
  done
}

main "$@"

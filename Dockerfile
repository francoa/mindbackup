# syntax=docker/dockerfile:1
#
# Two stages: build the venv, then copy just the venv into a slim runtime.
# The Whisper model is baked in at build time so the container never needs to
# reach Hugging Face at runtime (and a first memo isn't slowed by a download).

ARG PYTHON_VERSION=3.13
ARG WHISPER_MODEL=medium

# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# uv is pinned by digest-free tag but the lockfile is what actually pins deps.
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: only invalidated when the lock or manifest changes, so
# editing source doesn't trigger a full reinstall of torch-sized wheels.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --extra local

# Project layer.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra local

# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG WHISPER_MODEL

# ffmpeg decodes Telegram's ogg/opus. No recommends: keeps the image lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user. The vault is bind-mounted, so this uid must be able to
# write it — override with `user:` in compose if your host uid differs.
RUN groupadd --gid 1000 mindbackup \
    && useradd --uid 1000 --gid 1000 --create-home mindbackup

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_CACHE=/opt/models \
    HF_HUB_OFFLINE=1 \
    OBSIDIAN_VAULT_PATH=/vault \
    MINDBACKUP_AUDIO_ARCHIVE=/archive

COPY --from=builder --chown=mindbackup:mindbackup /app/.venv /app/.venv
COPY --chown=mindbackup:mindbackup src /app/src

# Bake the model into the image (needs network at BUILD time only).
# HF_HUB_OFFLINE is set above, so unset it just for this layer.
RUN mkdir -p /opt/models \
    && HF_HUB_OFFLINE=0 python -c "\
from faster_whisper import download_model; \
download_model('${WHISPER_MODEL}', cache_dir='/opt/models')" \
    && chown -R mindbackup:mindbackup /opt/models

WORKDIR /app
USER mindbackup

# Fails the build if the CLI can't even start.
RUN mindbackup --help > /dev/null

# Reports unhealthy if config/vault/STT preconditions break at runtime.
HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD mindbackup doctor || exit 1

ENTRYPOINT ["mindbackup"]
CMD ["bot"]

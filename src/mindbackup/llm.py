"""Minimal OpenAI-compatible chat client.

Deliberately hand-rolled on `urllib` rather than pulling in `openai`: the
whole project is dependency-shy (see config.py's docstring), and all we need
is one POST that returns JSON. Works against OpenRouter, OpenAI, Groq,
together, Ollama's /v1 shim — anything speaking /chat/completions.

Never used in the ingest hot path. Extraction runs *after* the memo is safely
in the vault, so an LLM outage degrades a memo to "unextracted", never to
"lost" (spec C4).
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class LLMError(Exception):
    """The model could not be reached or returned something unusable.

    Message is safe to show the user in Telegram.
    """


def _post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def complete(
    settings: Settings,
    system: str,
    user: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 4000,
) -> str:
    """One chat completion. Returns the assistant's text content."""
    settings.validate_for_llm()

    url = f"{settings.llm_base_url}/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            data = _post(url, payload, headers, settings.llm_timeout)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            last_error = f"HTTP {exc.code} from {settings.llm_base_url}: {body or exc.reason}"
            if exc.code == 401:
                raise LLMError(
                    "LLM rejected the API key (401). Check MINDBACKUP_LLM_API_KEY."
                ) from exc
            if exc.code not in RETRY_STATUSES:
                raise LLMError(last_error) from exc
        except urllib.error.URLError as exc:
            last_error = f"Cannot reach {settings.llm_base_url}: {exc.reason}"
        except (TimeoutError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            try:
                return data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"Unexpected LLM response shape: {str(data)[:300]}") from exc

        if attempt < MAX_ATTEMPTS:
            delay = 2 ** attempt
            logger.warning("LLM attempt %d/%d failed (%s); retrying in %ds",
                           attempt, MAX_ATTEMPTS, last_error, delay)
            time.sleep(delay)

    raise LLMError(f"LLM failed after {MAX_ATTEMPTS} attempts. {last_error}")


def _strip_code_fence(text: str) -> str:
    """Models wrap JSON in ```json fences no matter how firmly you ask."""
    fence = re.match(r"^\s*```(?:json)?\s*\n(.*?)\n?\s*```\s*$", text, re.DOTALL)
    return fence.group(1) if fence else text.strip()


def complete_json(
    settings: Settings,
    system: str,
    user: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 4000,
) -> Any:
    """Chat completion parsed as JSON, tolerating fences and prose padding."""
    raw = complete(
        settings, system, user, temperature=temperature, max_tokens=max_tokens
    )
    candidate = _strip_code_fence(raw)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Last resort: the outermost {...} or [...] in the reply.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise LLMError(f"LLM did not return JSON. Got: {raw[:300]}")


__all__ = ["LLMError", "complete", "complete_json"]

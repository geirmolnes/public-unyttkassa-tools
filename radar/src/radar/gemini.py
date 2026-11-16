"""Shared Gemini client for the two LLM steps: classification and clustering.

Both need the same three things — a lazily built client, a way to ask for
minimal thinking that survives model differences, and error text with the API
key scrubbed out before it reaches a log or the database.
"""

from __future__ import annotations

import json
import logging
import re
import threading

from google import genai
from google.genai import types

from . import config, usage_client

logger = logging.getLogger(__name__)

# Prefix of a GeminiError for a content block; storage filters on it too.
BLOCKED_PREFIX = "blocked response: "

_client: genai.Client | None = None

# What this tick has spent. Counted here rather than returned alongside every
# verdict: three call sites in two modules would each have to thread it through,
# and the number is only ever read once, at the end of the tick. Locked because
# classification and coverage both run the pool at CLASSIFY_WORKERS.
_usage_lock = threading.Lock()
_usage = {"calls": 0, "tokens": 0}


def usage() -> dict[str, int]:
    """Calls made and tokens spent since the last reset."""
    with _usage_lock:
        return dict(_usage)


def reset_usage() -> None:
    with _usage_lock:
        _usage.update(calls=0, tokens=0)


# How to ask for minimal thinking, newest spelling first. 3.5 rejects
# thinking_budget outright (a bare 400 INVALID_ARGUMENT that names no field),
# while older models don't know thinking_level — so the working variant is
# found once on the first call and reused for the rest of the run.
_THINKING_VARIANTS = [
    lambda: types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
    lambda: types.ThinkingConfig(thinking_budget=0),
    lambda: None,
]
_variant = 0

# Guards both the lazy client and the variant probe. Neither is hot — the client
# is read once per call and the variant settles after the first response — so
# one lock for both costs nothing and removes a whole class of first-tick race.
_client_lock = threading.Lock()


class GeminiError(RuntimeError):
    """A call that failed, with the key already redacted out of the message."""


def client() -> genai.Client:
    """The one shared client. Built once, under a lock, and that matters.

    This was an unsynchronised lazy singleton, and it produced a real failure:
    «Cannot send a request, as the client has been closed», one article per
    fresh container, always in the first batch classification.

    The chain. `classify_pending` runs the pool at CLASSIFY_WORKERS, so on the
    first call all eight workers see `_client is None` together and each builds
    its own `genai.Client`; the last assignment wins and the other seven become
    unreachable. `genai.Client.__del__` calls `close()`, which closes that
    client's httpx transport — and `generate_json` evaluates `client().models`,
    where `Models` holds only the *BaseApiClient*, never the wrapper. So the
    wrapper is collected while a call is still in flight through its transport,
    and the request that was already on its way raises.

    Reproduced at eight threads: eight clients built, seven of them left holding
    a transport that had been closed underneath them. A double-checked lock
    means exactly one client is ever constructed, so there is never an orphan to
    collect. The fast path stays a plain read of the global.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if not config.GEMINI_API_KEY:
                    raise RuntimeError("GEMINI_API_KEY is not set")
                # The SDK never retries unless asked. With this it retries 429/5xx, timeouts
                # and failed connects (TLS handshake errors included), which each used to
                # fail an article until the next tick and page the admin hub.
                _client = genai.Client(
                    api_key=config.GEMINI_API_KEY,
                    http_options=types.HttpOptions(
                        retry_options=types.HttpRetryOptions(attempts=3)
                    ),
                )
    return _client


# What a Google API key looks like, matched on shape rather than on the value we
# happen to hold. The value alone is not enough: a transport error quotes the
# offending header back through repr(), so a key that arrived with a stray
# newline is written out with the newline escaped and no longer equals anything
# we can compare against — which is exactly how one reached a log in full.
# Matching the shape also covers a key from somewhere other than our config.
_KEY_SHAPED = re.compile(r"AIza[0-9A-Za-z_-]{35}")


def redact(text: str) -> str:
    """Strip anything key-shaped out of error text.

    Transport errors quote the offending header verbatim, key included, and
    these strings get logged and written to the verdicts table.
    """
    if config.GEMINI_API_KEY and config.GEMINI_API_KEY in text:
        text = text.replace(config.GEMINI_API_KEY, "***")
    return _KEY_SHAPED.sub("***", text)


def _generation_config(system_prompt: str, schema: dict) -> types.GenerateContentConfig:
    kwargs = {
        "system_instruction": system_prompt,
        "temperature": 0,
        "response_mime_type": "application/json",
        "response_schema": schema,
        # News classification must handle reporting on sensitive subjects.
        # OFF is the least restrictive adjustable setting; Google's built-in
        # protections still apply and explicit blocks remain unclassified.
        "safety_settings": [
            types.SafetySetting(category=category, threshold=types.HarmBlockThreshold.OFF)
            for category in (
                types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            )
        ],
    }
    thinking = _THINKING_VARIANTS[_variant]()
    if thinking is not None:
        kwargs["thinking_config"] = thinking
    return types.GenerateContentConfig(**kwargs)


def generate_json(system_prompt: str, schema: dict, contents: str) -> dict:
    """One JSON-schema'd call. Raises GeminiError; never leaks the key."""
    global _variant
    failed_variant = _variant
    try:
        response = client().models.generate_content(
            model=config.GEMINI_MODEL,
            contents=contents,
            config=_generation_config(system_prompt, schema),
        )
    except Exception as e:  # noqa: BLE001 — surfaced as GeminiError for the caller to record
        usage_client.record(model=config.GEMINI_MODEL, kind="cron", status="error")
        message = redact(str(e))
        if "INVALID_ARGUMENT" in message and _variant + 1 < len(_THINKING_VARIANTS):
            # Eight workers hit the same rejection at once on the first batch.
            # Unguarded, each would advance the probe and the pool could skip
            # straight past the variant that works. Advance once, inside the
            # lock, and only from the variant that actually failed. The
            # recursion is outside the lock — it calls back into client().
            with _client_lock:
                if _variant == failed_variant and _variant + 1 < len(_THINKING_VARIANTS):
                    _variant += 1
                    logger.info("model rejected thinking config, trying variant %d", _variant)
            return generate_json(system_prompt, schema, contents)
        raise GeminiError(message[:300]) from e

    usage_client.record(response, model=config.GEMINI_MODEL, kind="cron")
    _record_usage(response)
    feedback = getattr(response, "prompt_feedback", None)
    reason = getattr(feedback, "block_reason", None)
    if reason and reason != "BLOCKED_REASON_UNSPECIFIED":
        raise GeminiError(f"{BLOCKED_PREFIX}{getattr(reason, 'value', reason)}")
    for candidate in getattr(response, "candidates", None) or []:
        reason = getattr(candidate, "finish_reason", None)
        if reason in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}:
            raise GeminiError(f"{BLOCKED_PREFIX}{getattr(reason, 'value', reason)}")
    if not response.text:
        reasons = [
            str(getattr(candidate, "finish_reason", "unknown"))
            for candidate in getattr(response, "candidates", None) or []
        ]
        raise GeminiError(f"empty response: finish reasons {', '.join(reasons) or 'missing'}")
    try:
        return json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as e:
        raise GeminiError(f"unparseable response: {e}") from e


def _record_usage(response) -> None:
    """Count one billable call. A response that answered still cost tokens."""
    meta = getattr(response, "usage_metadata", None)
    tokens = getattr(meta, "total_token_count", None) or 0
    with _usage_lock:
        _usage["calls"] += 1
        _usage["tokens"] += int(tokens)

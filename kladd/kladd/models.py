"""Model selection and the fallback chain for failed LLM calls."""

import logging
import os

import anthropic
import httpx
import openai
from google.genai.errors import APIError
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior

log = logging.getLogger(__name__)

PRIMARY = "google-gla:gemini-3.8-flash"

# Any failure on the primary goes straight to the first fallback, then the
# second. Both run outside Google, so a Gemini outage, a 503 capacity spike
# and a PROHIBITED_CONTENT false positive (Gemini's own non-configurable
# filter) all land on a different stack. Picked by eval 2026-10-02: Sonnet
# matched Gemini on every check; Luna is fast but paraphrases more quotes.
FALLBACKS = tuple(
    os.environ.get(
        "KLADD_FALLBACK_MODELS",
        "anthropic:claude-sonnet-5-5,openrouter:openai/gpt-6-luna",
    ).split(",")
)

# Gemini 3.x replaced the numeric thinking_budget with thinking_level. "low"
# is the floor for 3.8-flash — it rejects "minimal" with "not supported for
# this model".
THINKING_LEVEL = "low"

# Settings for "openrouter:<vendor>/<model>" names. OpenRouter also serves
# Claude through Google Vertex, which would put a non-Google fallback back
# in Google's failure domain. Source material can be unpublished, so skip
# hosts that train on prompts. "low" is right for Sonnet 5.5 (adaptive, ~0
# thinking tokens, and reasoning can't be disabled) and Luna, but becomes a
# multi-thousand-token budget on Haiku 4.5 or Mistral — retune if swapping.
OPENROUTER_SETTINGS = {
    "openrouter_provider": {
        "ignore": ["google-vertex", "google-ai-studio"],
        "data_collection": "deny",
    },
    "openrouter_reasoning": {"effort": "low"},
}

# Settings for "anthropic:<model>" names (direct Claude API). Sonnet 5.5
# thinks adaptively and can't turn it off; "low" effort keeps it near zero.
ANTHROPIC_SETTINGS = {"anthropic_effort": "low"}


def run_settings(model: str) -> dict:
    """Per-call settings that can't live in the shared, prefix-merged dict.

    Claude 5.5 rejects any explicit temperature with a 400, and pydantic-ai
    caps Anthropic output at 4096 tokens unless max_tokens is set.
    """
    if model.startswith("anthropic:"):
        return {"max_tokens": 16000}
    return {"temperature": 0.0}

# Everything that means "this model didn't produce an answer": pydantic-ai's
# wrappers (HTTP status, connection, content filter, empty or invalid output)
# plus what the streaming paths let through unwrapped — google-genai's
# APIError, anthropic's and openai's (OpenRouter) APIError, and dropped
# connections.
RECOVERABLE_ERRORS = (
    ModelAPIError,
    UnexpectedModelBehavior,
    APIError,
    anthropic.APIError,
    openai.APIError,
    httpx.TransportError,
)


# A fallback without its key raises pydantic-ai's UserError, which isn't
# recoverable and would end the chain early — skip it instead.
_KEY_ENV = {"anthropic:": "ANTHROPIC_API_KEY", "openrouter:": "OPENROUTER_API_KEY"}


def _has_key(model: str) -> bool:
    return all(os.environ.get(env) for prefix, env in _KEY_ENV.items() if model.startswith(prefix))


def retry_model(exc: Exception, current: str) -> str | None:
    """Next model down the chain after `current` failed, or None to give up.

    A model outside the chain (requested per call, e.g. by evals) falls back
    to the first fallback, like the primary does. Fallbacks missing their
    API key are skipped.
    """
    if not isinstance(exc, RECOVERABLE_ERRORS):
        return None
    chain = (PRIMARY, *FALLBACKS)
    position = chain.index(current) if current in chain else 0
    for candidate in chain[position + 1:]:
        if _has_key(candidate):
            return candidate
        log.warning("Skipping fallback %s: API key not set", candidate)
    return None

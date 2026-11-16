"""LLM wrapper for article drafting."""

import logging
from collections.abc import AsyncIterator

from pydantic_ai import Agent

from . import config, run_usage
from .metered import metered
from .models import (
    ANTHROPIC_SETTINGS,
    OPENROUTER_SETTINGS,
    PRIMARY,
    RECOVERABLE_ERRORS,
    THINKING_LEVEL,
    retry_model,
    run_settings,
)
from .prompt import build_user_prompt, system_prompt

log = logging.getLogger(__name__)

# Disable the four configurable safety categories. Does NOT affect
# PROHIBITED_CONTENT (separate non-configurable filter) — that's what
# the content-filter retry in models.retry_model is for.
_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# One process serves one product, but målform follows the source material,
# so both agents (bokmål + nynorsk) exist and are picked per draft.
# temperature comes per call from run_settings() — Claude rejects it.
_MODEL_SETTINGS = {
    "google_thinking_config": {"thinking_level": THINKING_LEVEL},
    "google_safety_settings": _SAFETY_SETTINGS,
    # Prefixed keys are read only by their own model class, so one dict
    # serves Gemini and a fallback model passed per request alike.
    **OPENROUTER_SETTINGS,
    **ANTHROPIC_SETTINGS,
}

_agents = {
    nynorsk: Agent(
        metered(PRIMARY),
        instructions=system_prompt(manus=config.IS_MANUS, nynorsk=nynorsk),
        model_settings=_MODEL_SETTINGS,
    )
    for nynorsk in (False, True)
}


async def draft_article(
    formatted_sources: str,
    source_count: int,
    *,
    all_urls: bool = False,
    model: str | None = None,
    instructions: str | None = None,
    rules: list[str] | None = None,
    nynorsk: bool = False,
) -> tuple[str, str]:
    """Draft a Norwegian news article. Returns (article, model actually used)."""
    user_prompt = build_user_prompt(
        formatted_sources,
        source_count,
        all_urls=all_urls,
        manus=config.IS_MANUS,
        instructions=instructions,
        rules=rules,
    )
    _agent = _agents[nynorsk]
    model = model or PRIMARY
    while True:
        try:
            result = await _agent.run(
                user_prompt, model=metered(model), model_settings=run_settings(model)
            )
            run_usage.add(model, "draft", result)
            return result.output, model
        except RECOVERABLE_ERRORS as exc:
            # End of the fallback chain — let the caller surface it
            retry = retry_model(exc, model)
            if retry is None:
                raise
            log.warning("%s failed on %s; retrying with %s", type(exc).__name__, model, retry)
            model = retry


async def stream_draft_article(
    formatted_sources: str,
    source_count: int,
    *,
    all_urls: bool = False,
    instructions: str | None = None,
    rules: list[str] | None = None,
    nynorsk: bool = False,
    meta: dict | None = None,
) -> AsyncIterator[str]:
    """Stream delta chunks for a Norwegian news article.

    If `meta` is given, meta["model"] is set to the model that actually
    produced the output (fallback may differ from PRIMARY).
    """
    user_prompt = build_user_prompt(
        formatted_sources,
        source_count,
        all_urls=all_urls,
        manus=config.IS_MANUS,
        instructions=instructions,
        rules=rules,
    )
    _agent = _agents[nynorsk]
    model = PRIMARY
    while True:
        if meta is not None:
            meta["model"] = model
        yielded_any = False
        try:
            # None keeps the agent's own (metered) primary instance
            run_model = None if model == PRIMARY else metered(model)
            async with _agent.run_stream(
                user_prompt, model=run_model, model_settings=run_settings(model)
            ) as stream:
                async for chunk in stream.stream_text(delta=True):
                    yielded_any = True
                    yield chunk
            return
        except RECOVERABLE_ERRORS as exc:
            # Retry only if nothing has reached the client yet
            retry = None if yielded_any else retry_model(exc, model)
            if retry is None:
                raise
            log.warning("%s failed on %s; retrying with %s", type(exc).__name__, model, retry)
            model = retry

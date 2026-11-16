"""Token usage per LLM call within one request, returned by /api/generate for eval cost tracking."""

from contextvars import ContextVar

from pydantic_ai.agent import AgentRunResult

_calls: ContextVar[list[dict] | None] = ContextVar("run_usage", default=None)


def start() -> list[dict]:
    """Begin collecting usage for the current request; returns the list that fills up."""
    calls: list[dict] = []
    _calls.set(calls)
    return calls


def add(model: str, step: str, result: AgentRunResult) -> None:
    """Record one successful agent run. No-op outside a collecting request."""
    calls = _calls.get()
    if calls is None:
        return
    usage = result.usage
    details = usage.details or {}
    calls.append({
        "model": model,
        "step": step,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        # Gemini reports thoughts_tokens, OpenAI/OpenRouter reasoning_tokens
        "thinking_tokens": details.get("thoughts_tokens") or details.get("reasoning_tokens") or 0,
        "requests": usage.requests,
    })

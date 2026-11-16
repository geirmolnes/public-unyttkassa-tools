import asyncio
from types import SimpleNamespace

from pydantic_ai.usage import RunUsage

from kladd import run_usage


def _result(**kw) -> SimpleNamespace:
    return SimpleNamespace(usage=RunUsage(**kw))


def test_add_outside_request_is_noop():
    run_usage._calls.set(None)
    run_usage.add("google-gla:gemini-3.8-flash", "draft", _result(input_tokens=1))
    assert run_usage._calls.get() is None


def test_collects_tokens_and_thinking_per_call():
    async def scenario():
        calls = run_usage.start()
        run_usage.add(
            "google-gla:gemini-3.8-flash", "draft",
            _result(input_tokens=1000, output_tokens=300, details={"thoughts_tokens": 120}, requests=1),
        )
        # Child tasks (gather) copy the context but share the list
        await asyncio.gather(asyncio.to_thread(
            run_usage.add, "openrouter:openai/gpt-6-luna", "quote_verify",
            _result(input_tokens=50, output_tokens=5, details={"reasoning_tokens": 7}),
        ))
        return calls

    calls = asyncio.run(scenario())
    assert [(c["step"], c["input_tokens"], c["output_tokens"], c["thinking_tokens"]) for c in calls] == [
        ("draft", 1000, 300, 120),
        ("quote_verify", 50, 5, 7),
    ]


def test_missing_details_means_zero_thinking():
    async def scenario():
        calls = run_usage.start()
        run_usage.add("anthropic:claude-sonnet-5-5", "draft", _result(input_tokens=10, output_tokens=2))
        return calls

    assert asyncio.run(scenario())[0]["thinking_tokens"] == 0

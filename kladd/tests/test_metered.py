import asyncio

import pytest

from google.genai.types import (
    GenerateContentResponse,
    GenerateContentResponseUsageMetadata,
)
from pydantic_ai.models.google import GoogleModel

from kladd import metered


def test_records_provider_response_before_validation(monkeypatch):
    response = GenerateContentResponse(usage_metadata=GenerateContentResponseUsageMetadata(
        prompt_token_count=100, candidates_token_count=10, thoughts_token_count=20))
    async def fake(*args, **kwargs):
        return response
    records = []
    monkeypatch.setattr(GoogleModel, "_generate_content", fake)
    monkeypatch.setattr(metered, "record", lambda *a, **kw: records.append((a, kw)))
    # Avoid requiring provider credentials while exercising the real subclass method.
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    model = metered.MeteredGoogleModel("gemini-3.8-flash", provider="google-gla")
    result = asyncio.run(model._generate_content([], False, {}, None))
    assert result is response
    assert len(records) == 1
    assert records[0][0][0].usage_metadata.thoughts_token_count == 20


def test_stream_uses_last_usage_snapshot_once(monkeypatch):
    chunks = [GenerateContentResponse(usage_metadata=GenerateContentResponseUsageMetadata(
        prompt_token_count=100, candidates_token_count=n)) for n in (1, 5, 10)]
    async def fake(*args, **kwargs):
        async def stream():
            for chunk in chunks:
                yield chunk
        return stream()
    records = []
    monkeypatch.setattr(GoogleModel, "_generate_content", fake)
    monkeypatch.setattr(metered, "record", lambda *a, **kw: records.append((a, kw)))
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    model = metered.MeteredGoogleModel("gemini-3.8-flash", provider="google-gla")
    async def run():
        stream = await model._generate_content([], True, {}, None)
        assert [c async for c in stream] == chunks
    asyncio.run(run())
    assert len(records) == 1
    assert records[0][0][0].usage_metadata.candidates_token_count == 10


def _usage(prompt=1000, completion=300, reasoning=120, cached=200):
    from openai.types.completion_usage import (
        CompletionTokensDetails,
        CompletionUsage,
        PromptTokensDetails,
    )
    return CompletionUsage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion,
        completion_tokens_details=CompletionTokensDetails(reasoning_tokens=reasoning),
        prompt_tokens_details=PromptTokensDetails(cached_tokens=cached))


def test_gemini_shaped_splits_reasoning_out_of_completion():
    meta = metered.gemini_shaped(_usage())["usage_metadata"]
    assert meta == {"prompt_token_count": 1000, "candidates_token_count": 180,
                    "thoughts_token_count": 120, "cached_content_token_count": 200,
                    "total_token_count": 1300}


def test_openrouter_records_response_before_validation(monkeypatch):
    from openai.types.chat import ChatCompletion
    from pydantic_ai.models.openrouter import OpenRouterModel
    response = ChatCompletion(id="x", choices=[], created=0, model="anthropic/claude-sonnet-5.5",
                              object="chat.completion", usage=_usage())
    async def fake(*args, **kwargs):
        return response
    records = []
    monkeypatch.setattr(OpenRouterModel, "_completions_create", fake)
    monkeypatch.setattr(metered, "record", lambda *a, **kw: records.append((a, kw)))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    model = metered.MeteredOpenRouterModel("anthropic/claude-sonnet-5.5")
    assert asyncio.run(model._completions_create([], False, {}, None)) is response
    assert len(records) == 1
    assert records[0][0][0]["usage_metadata"]["thoughts_token_count"] == 120
    assert records[0][1]["model"] == "openrouter:anthropic/claude-sonnet-5.5"


def test_openrouter_stream_records_final_usage_chunk_once(monkeypatch):
    from types import SimpleNamespace

    from pydantic_ai.models.openrouter import OpenRouterModel
    chunks = [SimpleNamespace(usage=None), SimpleNamespace(usage=None),
              SimpleNamespace(usage=_usage(completion=50, reasoning=0))]

    class FakeAsyncStream:
        entered = exited = False
        async def __aenter__(self):
            self.entered = True
            return self
        async def __aexit__(self, *exc_info):
            self.exited = True
        async def __aiter__(self):
            for chunk in chunks:
                yield chunk

    raw = FakeAsyncStream()
    async def fake(*args, **kwargs):
        return raw
    records = []
    monkeypatch.setattr(OpenRouterModel, "_completions_create", fake)
    monkeypatch.setattr(metered, "record", lambda *a, **kw: records.append((a, kw)))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    model = metered.MeteredOpenRouterModel("openai/gpt-6-luna")
    async def run():
        stream = await model._completions_create([], True, {}, None)
        async with stream:
            assert [c async for c in stream] == chunks
    asyncio.run(run())
    assert raw.entered and raw.exited
    assert len(records) == 1
    assert records[0][0][0]["usage_metadata"]["candidates_token_count"] == 50
    assert records[0][1]["status"] == "ok"


def test_metered_picks_model_class_by_prefix(monkeypatch):
    monkeypatch.setenv("AI_USAGE_TOKEN", "test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert isinstance(metered.metered("anthropic:claude-sonnet-5-5"), metered.MeteredAnthropicModel)
    assert isinstance(metered.metered("openrouter:openai/gpt-6-luna"), metered.MeteredOpenRouterModel)
    assert isinstance(metered.metered("google-gla:gemini-3.8-flash"), metered.MeteredGoogleModel)
    monkeypatch.delenv("AI_USAGE_TOKEN")
    assert metered.metered("openrouter:openai/gpt-6-luna") == "openrouter:openai/gpt-6-luna"


def _anthropic_usage(inp=1000, out=300, read=200, write=50):
    from anthropic.types.beta import BetaUsage
    return BetaUsage(input_tokens=inp, output_tokens=out,
                     cache_read_input_tokens=read, cache_creation_input_tokens=write)


@pytest.mark.parametrize(
    "usage,expected",
    [
        (_anthropic_usage(), {"prompt_token_count": 1250, "candidates_token_count": 300,
                              "cached_content_token_count": 200, "total_token_count": 1550}),
        (_anthropic_usage(read=None, write=None), {
            "prompt_token_count": 1000, "candidates_token_count": 300,
            "cached_content_token_count": 0, "total_token_count": 1300}),
    ],
    ids=["with-cache", "no-cache-fields"],
)
def test_anthropic_shaped_adds_cache_back_into_prompt(usage, expected):
    assert metered.anthropic_shaped(usage)["usage_metadata"] == expected


def test_anthropic_shaped_none():
    assert metered.anthropic_shaped(None) is None


def test_anthropic_records_response_before_validation(monkeypatch):
    from types import SimpleNamespace

    from pydantic_ai.models.anthropic import AnthropicModel
    response = SimpleNamespace(usage=_anthropic_usage(read=0, write=0))
    async def fake(*args, **kwargs):
        return response
    records = []
    monkeypatch.setattr(AnthropicModel, "_messages_create", fake)
    monkeypatch.setattr(metered, "record", lambda *a, **kw: records.append((a, kw)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    model = metered.MeteredAnthropicModel("claude-sonnet-5-5")
    assert asyncio.run(model._messages_create([], False, {}, None)) is response
    assert len(records) == 1
    assert records[0][0][0]["usage_metadata"]["candidates_token_count"] == 300
    assert records[0][1]["model"] == "anthropic:claude-sonnet-5-5"


@pytest.mark.parametrize(
    "consume_all,expected_output,expected_status",
    [(True, 42, "ok"), (False, 7, "cancelled")],
    ids=["completed", "cancelled-mid-stream"],
)
def test_anthropic_stream_merges_start_and_delta_usage_once(
    monkeypatch, consume_all, expected_output, expected_status
):
    from types import SimpleNamespace

    from pydantic_ai.models.anthropic import AnthropicModel
    events = [
        SimpleNamespace(type="message_start",
                        message=SimpleNamespace(usage=_anthropic_usage(out=1, read=0, write=0))),
        SimpleNamespace(type="content_block_delta"),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=7)),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=42)),
    ]

    class FakeAsyncStream:
        exited = False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc_info):
            self.exited = True
        async def __aiter__(self):
            for event in events:
                yield event

    raw = FakeAsyncStream()
    async def fake(*args, **kwargs):
        return raw
    records = []
    monkeypatch.setattr(AnthropicModel, "_messages_create", fake)
    monkeypatch.setattr(metered, "record", lambda *a, **kw: records.append((a, kw)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    model = metered.MeteredAnthropicModel("claude-sonnet-5-5")
    async def run():
        stream = await model._messages_create([], True, {}, None)
        async with stream:
            seen = 0
            async for _ in stream:
                seen += 1
                if not consume_all and seen == 3:
                    break
    asyncio.run(run())
    assert raw.exited
    assert len(records) == 1
    meta = records[0][0][0]["usage_metadata"]
    assert (meta["prompt_token_count"], meta["candidates_token_count"]) == (1000, expected_output)
    assert records[0][1]["status"] == expected_status

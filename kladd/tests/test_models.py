"""Tests for the model fallback chain: Gemini 3.8 Flash → Sonnet 5.5 → GPT-6 Luna."""

from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
from google.genai.errors import ClientError, ServerError
from pydantic_ai import PromptedOutput
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)

from kladd import drafter, quote_verify
from kladd.models import retry_model, run_settings

PRIMARY = "google-gla:gemini-3.8-flash"
SONNET = "anthropic:claude-sonnet-5-5"
LUNA = "openrouter:openai/gpt-6-luna"


def http_error(status: int, model: str = PRIMARY) -> ModelHTTPError:
    """What the non-streaming paths raise — pydantic-ai's wrapper."""
    return ModelHTTPError(status, model, {"error": {"code": status}})


def genai_error(status: int) -> ServerError | ClientError:
    """What the Gemini streaming path raises — google-genai's own, unwrapped."""
    body = {"error": {"code": status, "status": "UNAVAILABLE"}}
    cls = ServerError if status >= 500 else ClientError
    return cls(status, body)


def anthropic_error() -> anthropic.APIError:
    """What a direct Claude stream can raise mid-response, unwrapped."""
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))


def openai_error() -> openai.APIError:
    """What an OpenRouter stream can raise mid-response, unwrapped."""
    return openai.APIConnectionError(request=httpx.Request("POST", "https://openrouter.ai"))


@pytest.mark.parametrize(
    "exc,current,expected",
    [
        (http_error(503), PRIMARY, SONNET),
        (http_error(500), PRIMARY, SONNET),
        (http_error(429), PRIMARY, SONNET),
        (http_error(400), PRIMARY, SONNET),
        (genai_error(503), PRIMARY, SONNET),
        (ContentFilterError("blocked"), PRIMARY, SONNET),
        (httpx.ConnectError("refused"), PRIMARY, SONNET),
        (http_error(503, SONNET), SONNET, LUNA),
        (anthropic_error(), SONNET, LUNA),
        (openai_error(), LUNA, None),
        (UnexpectedModelBehavior("empty response"), SONNET, LUNA),
        (http_error(503, LUNA), LUNA, None),
        (http_error(503), "google-gla:gemini-3.5-flash", SONNET),
        (ValueError("unrelated"), PRIMARY, None),
        (TimeoutError(), PRIMARY, None),
    ],
    ids=[
        "503-capacity",
        "500-server-error",
        "429-rate-limit",
        "400-bad-request",
        "genai-503-streaming",
        "content-filter",
        "connection-refused",
        "sonnet-fails",
        "anthropic-stream-error",
        "openrouter-stream-error-end-of-chain",
        "sonnet-empty-response",
        "end-of-chain",
        "off-chain-requested-model",
        "unrelated-exception",
        "timeout",
    ],
)
def test_retry_model(exc, current, expected):
    assert retry_model(exc, current) == expected


class FakeStream:
    """Stands in for pydantic-ai's streamed run result."""

    def __init__(self, chunks: list[str], fail_after: Exception | None = None):
        self.chunks = chunks
        self.fail_after = fail_after

    async def stream_text(self, delta: bool = True):
        for chunk in self.chunks:
            yield chunk
        if self.fail_after:
            raise self.fail_after


class FakeRunStream:
    """Async CM returned by Agent.run_stream — raises on enter when the request fails."""

    def __init__(self, stream: FakeStream | None, enter_error: Exception | None = None):
        self.stream = stream
        self.enter_error = enter_error

    async def __aenter__(self):
        if self.enter_error:
            raise self.enter_error
        return self.stream

    async def __aexit__(self, *exc_info):
        return False


async def test_draft_article_falls_back_to_sonnet(monkeypatch):
    """Any failure on the primary goes straight to Sonnet."""
    models_called = []

    async def fake_run(prompt, *, model=None, **kwargs):
        models_called.append(model)
        if len(models_called) == 1:
            raise http_error(503, model)
        return SimpleNamespace(output="Artikkeltekst")

    monkeypatch.setattr(drafter._agents[False], "run", fake_run)

    article, used = await drafter.draft_article("kildetekst", 1, all_urls=True)

    assert (article, used) == ("Artikkeltekst", SONNET)
    assert models_called == [PRIMARY, SONNET]


async def test_draft_article_falls_back_to_luna_when_sonnet_fails(monkeypatch):
    models_called = []

    async def fake_run(prompt, *, model=None, **kwargs):
        models_called.append(model)
        if model != LUNA:
            raise http_error(500, model)
        return SimpleNamespace(output="Artikkeltekst")

    monkeypatch.setattr(drafter._agents[False], "run", fake_run)

    article, used = await drafter.draft_article("kildetekst", 1)

    assert (article, used) == ("Artikkeltekst", LUNA)
    assert models_called == [PRIMARY, SONNET, LUNA]


async def test_draft_article_raises_when_chain_exhausted(monkeypatch):
    models_called = []

    async def fake_run(prompt, *, model=None, **kwargs):
        models_called.append(model)
        raise http_error(503, model)

    monkeypatch.setattr(drafter._agents[False], "run", fake_run)

    with pytest.raises(ModelHTTPError) as exc_info:
        await drafter.draft_article("kildetekst", 1)

    assert exc_info.value.model_name == LUNA
    assert models_called == [PRIMARY, SONNET, LUNA]


async def test_draft_article_does_not_retry_bugs(monkeypatch):
    """A non-LLM exception is our bug, not a model outage — no fallback."""
    models_called = []

    async def fake_run(prompt, *, model=None, **kwargs):
        models_called.append(model)
        raise ValueError("bug")

    monkeypatch.setattr(drafter._agents[False], "run", fake_run)

    with pytest.raises(ValueError):
        await drafter.draft_article("kildetekst", 1)

    assert models_called == [PRIMARY]


async def test_stream_falls_back_before_first_chunk(monkeypatch):
    """Nothing has reached the client yet, so the stream restarts on the next model."""
    models_called = []

    def fake_run_stream(prompt, *, model=None, **kwargs):
        models_called.append(model or PRIMARY)
        if len(models_called) == 1:
            # The streaming path surfaces google-genai's error, not pydantic-ai's
            return FakeRunStream(None, enter_error=genai_error(503))
        if len(models_called) == 2:
            return FakeRunStream(None, enter_error=anthropic_error())
        return FakeRunStream(FakeStream(["Ordfører", "en sier nei."]))

    monkeypatch.setattr(drafter._agents[False], "run_stream", fake_run_stream)

    meta: dict = {}
    chunks = [c async for c in drafter.stream_draft_article("kildetekst", 1, meta=meta)]

    assert chunks == ["Ordfører", "en sier nei."]
    assert meta["model"] == LUNA
    assert models_called == [PRIMARY, SONNET, LUNA]


async def test_stream_reraises_after_first_chunk(monkeypatch):
    """Restarting mid-stream would duplicate text already sent — propagate instead."""
    models_called = []

    def fake_run_stream(prompt, *, model=None, **kwargs):
        models_called.append(model or PRIMARY)
        return FakeRunStream(FakeStream(["Ordfør"], fail_after=genai_error(503)))

    monkeypatch.setattr(drafter._agents[False], "run_stream", fake_run_stream)

    meta: dict = {}
    received = []
    with pytest.raises(ServerError):
        async for chunk in drafter.stream_draft_article("kildetekst", 1, meta=meta):
            received.append(chunk)

    assert received == ["Ordfør"]
    assert models_called == [PRIMARY]


async def test_verify_quotes_falls_back_with_prompted_output(monkeypatch):
    """Sonnet rejects forced tool calls, so non-Gemini verifiers use prompted JSON."""
    calls = []
    verdict = quote_verify.QuoteVerdict(
        quote="– Sitat", ok=True, source_snippet="Sitat", corrected_quote=None, reason="ok"
    )

    async def fake_run(prompt, *, model=None, output_type=None, **kwargs):
        calls.append((model or PRIMARY, output_type))
        if len(calls) == 1:
            raise ContentFilterError("blocked")
        return SimpleNamespace(output=quote_verify.VerifyResponse(verdicts=[verdict]))

    monkeypatch.setattr(quote_verify._agents[False], "run", fake_run)

    verdicts = await quote_verify.verify_quotes(["– Sitat"], ["Sitat"])

    assert verdicts == [verdict]
    assert [m for m, _ in calls] == [PRIMARY, SONNET]
    assert calls[0][1] is quote_verify.VerifyResponse
    assert isinstance(calls[1][1], PromptedOutput)


@pytest.mark.parametrize(
    "model,expected",
    [
        (PRIMARY, {"temperature": 0.0}),
        ("google-gla:gemini-3.5-flash", {"temperature": 0.0}),
        (SONNET, {"max_tokens": 16000}),
        ("anthropic:claude-opus-5-5", {"max_tokens": 16000}),
        (LUNA, {"temperature": 0.0}),
    ],
    ids=["primary", "other-gemini", "sonnet", "other-claude", "luna"],
)
def test_run_settings(model, expected):
    # Claude 5.5 400s on any explicit temperature; 4096 default would truncate
    assert run_settings(model) == expected


@pytest.mark.parametrize("module", [drafter, quote_verify], ids=["drafter", "quote_verify"])
def test_agents_carry_no_temperature(module):
    """Agent-level settings merge into every call, so a temperature there would reach Claude."""
    for agent in module._agents.values():
        assert "temperature" not in agent.model_settings


async def test_draft_fallback_to_claude_sends_no_temperature(monkeypatch):
    calls = []

    async def fake_run(prompt, *, model=None, model_settings=None, **kwargs):
        calls.append(model_settings)
        if len(calls) == 1:
            raise http_error(503, model)
        return SimpleNamespace(output="Artikkeltekst")

    monkeypatch.setattr(drafter._agents[False], "run", fake_run)

    await drafter.draft_article("kildetekst", 1)

    assert calls == [{"temperature": 0.0}, {"max_tokens": 16000}]


@pytest.mark.parametrize(
    "model,prompted",
    [(PRIMARY, False), (SONNET, True), (LUNA, True)],
    ids=["gemini-tool-output", "sonnet-prompted", "luna-prompted"],
)
def test_verify_output_type(model, prompted):
    assert isinstance(quote_verify._output_type(model), PromptedOutput) is prompted


@pytest.mark.parametrize(
    "missing,current,expected",
    [
        (["ANTHROPIC_API_KEY"], PRIMARY, LUNA),
        (["OPENROUTER_API_KEY"], PRIMARY, SONNET),
        (["OPENROUTER_API_KEY"], SONNET, None),
        (["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"], PRIMARY, None),
        (["ANTHROPIC_API_KEY"], "google-gla:gemini-3.5-flash", LUNA),
    ],
    ids=["no-claude-key", "no-openrouter-key", "no-openrouter-key-after-sonnet",
         "no-fallback-keys", "off-chain-no-claude-key"],
)
def test_retry_skips_fallbacks_without_key(monkeypatch, missing, current, expected):
    """A missing key must skip to the next model, not end the chain with a UserError."""
    for env in missing:
        monkeypatch.delenv(env, raising=False)
    assert retry_model(http_error(503), current) == expected


@pytest.mark.parametrize("empty", ["", None], ids=["empty-string", "unset"])
def test_empty_key_counts_as_missing(monkeypatch, empty):
    if empty is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", empty)
    assert retry_model(http_error(503), PRIMARY) == LUNA


async def test_stream_skips_claude_without_key(monkeypatch):
    """End to end through the drafter: no Claude key, Gemini down -> Luna answers."""
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    models_called = []

    def fake_run_stream(prompt, *, model=None, **kwargs):
        models_called.append(model or PRIMARY)
        if len(models_called) == 1:
            return FakeRunStream(None, enter_error=genai_error(503))
        return FakeRunStream(FakeStream(["Tekst"]))

    monkeypatch.setattr(drafter._agents[False], "run_stream", fake_run_stream)

    meta: dict = {}
    chunks = [c async for c in drafter.stream_draft_article("kildetekst", 1, meta=meta)]

    assert (chunks, meta["model"], models_called) == (["Tekst"], LUNA, [PRIMARY, LUNA])

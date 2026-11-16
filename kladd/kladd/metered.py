"""Capture raw provider usage before Pydantic AI transforms or validates it."""

import asyncio
import os

from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openrouter import OpenRouterModel

from .usage_client import record


class MeteredGoogleModel(GoogleModel):
    async def _generate_content(self, messages, stream, model_settings, model_request_parameters):
        try:
            response = await super()._generate_content(
                messages, stream, model_settings, model_request_parameters
            )
        except Exception:
            await asyncio.to_thread(record, model=self.model_name, kind="interactive", status="error")
            raise
        if not stream:
            await asyncio.to_thread(record, response, model=self.model_name, kind="interactive")
            return response

        async def measured_stream():
            latest = None
            completed = False
            try:
                async for chunk in response:
                    if chunk.usage_metadata is not None:
                        latest = chunk
                    yield chunk
                completed = True
            finally:
                await asyncio.to_thread(
                    record, latest, model=self.model_name, kind="interactive",
                    status="ok" if completed else "cancelled",
                )
        return measured_stream()


def gemini_shaped(usage):
    """OpenAI-style usage in the Gemini shape usage_client.record() reads.

    completion_tokens includes reasoning; Gemini reports thoughts separately
    and the dashboard prices output + thinking, so split them out to avoid
    charging reasoning twice.
    """
    if usage is None:
        return None
    reasoning = getattr(usage.completion_tokens_details, "reasoning_tokens", None) or 0
    cached = getattr(usage.prompt_tokens_details, "cached_tokens", None) or 0
    return {"usage_metadata": {
        "prompt_token_count": usage.prompt_tokens,
        "candidates_token_count": usage.completion_tokens - reasoning,
        "thoughts_token_count": reasoning,
        "cached_content_token_count": cached,
        "total_token_count": usage.total_tokens,
    }}


def anthropic_shaped(usage):
    """Anthropic usage in the Gemini shape usage_client.record() reads.

    input_tokens excludes cache reads/writes, so add them back; thinking is
    billed inside output_tokens and isn't reported separately.
    """
    if usage is None:
        return None
    cached = usage.cache_read_input_tokens or 0
    prompt = usage.input_tokens + cached + (usage.cache_creation_input_tokens or 0)
    return {"usage_metadata": {
        "prompt_token_count": prompt,
        "candidates_token_count": usage.output_tokens,
        "cached_content_token_count": cached,
        "total_token_count": prompt + usage.output_tokens,
    }}


class _MeteredStream:
    """Stands in for openai's AsyncStream, which pydantic-ai enters with
    `async with`; records the last usage chunk once when the stream closes."""

    def __init__(self, stream, model):
        self._stream, self._model = stream, model
        self._latest, self._completed = None, False

    def _observe(self, chunk):
        if chunk.usage is not None:
            self._latest = chunk

    def _shaped(self):
        return gemini_shaped(getattr(self._latest, "usage", None))

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        try:
            return await self._stream.__aexit__(*exc_info)
        finally:
            await asyncio.to_thread(
                record, self._shaped(),
                model=self._model, kind="interactive",
                status="ok" if self._completed else "cancelled",
            )

    async def __aiter__(self):
        async for chunk in self._stream:
            self._observe(chunk)
            yield chunk
        self._completed = True


class _MeteredAnthropicStream(_MeteredStream):
    """Anthropic splits usage: input counts on message_start, the running
    output count on each message_delta."""

    def _observe(self, event):
        if event.type == "message_start":
            self._latest = event.message.usage
        elif event.type == "message_delta" and self._latest is not None:
            self._latest = self._latest.model_copy(
                update={"output_tokens": event.usage.output_tokens}
            )

    def _shaped(self):
        return anthropic_shaped(self._latest)


class MeteredOpenRouterModel(OpenRouterModel):
    async def _completions_create(self, messages, stream, model_settings, model_request_parameters):
        name = f"openrouter:{self.model_name}"
        try:
            response = await super()._completions_create(
                messages, stream, model_settings, model_request_parameters
            )
        except Exception:
            await asyncio.to_thread(record, model=name, kind="interactive", status="error")
            raise
        if stream:
            return _MeteredStream(response, name)
        await asyncio.to_thread(
            record, gemini_shaped(getattr(response, "usage", None)), model=name, kind="interactive"
        )
        return response


class MeteredAnthropicModel(AnthropicModel):
    async def _messages_create(self, messages, stream, model_settings, model_request_parameters):
        name = f"anthropic:{self.model_name}"
        try:
            response = await super()._messages_create(
                messages, stream, model_settings, model_request_parameters
            )
        except Exception:
            await asyncio.to_thread(record, model=name, kind="interactive", status="error")
            raise
        if stream:
            return _MeteredAnthropicStream(response, name)
        await asyncio.to_thread(
            record, anthropic_shaped(response.usage), model=name, kind="interactive"
        )
        return response


def metered(name):
    if not os.getenv("AI_USAGE_TOKEN"):
        return name
    if name.startswith("anthropic:"):
        return MeteredAnthropicModel(name.removeprefix("anthropic:"))
    if name.startswith("openrouter:"):
        return MeteredOpenRouterModel(name.removeprefix("openrouter:"))
    return MeteredGoogleModel(name.removeprefix("google-gla:"), provider="google-gla")

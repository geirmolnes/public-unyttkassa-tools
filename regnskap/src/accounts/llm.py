"""Vision calls for page reads: Claude API (default Claude Haiku 5.5) with Gemini as fallback."""

import asyncio
import base64
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import anthropic
from google import genai
from google.genai import types
from pydantic import BaseModel

from src import usage

log = logging.getLogger(__name__)

MODEL = os.environ.get("ACCOUNTS_MODEL", "claude-haiku-5-5")
FALLBACK = os.environ.get("ACCOUNTS_FALLBACK", "gemini-3.5-flash-lite")
ATTEMPTS = 3
TIMEOUT = 300.0
# Price per 1M tokens (in, out). Claude Haiku 5.5 has two rate cards: prompts up to 100K tokens, and longer.
CLAUDE_PRICE = ((0.10, 0.50), (0.50, 2.50))
CLAUDE_LONG_PROMPT = 100_000
GEMINI_PRICE = (0.30, 2.50)
NO_KEY = "Tall-lesing er ikke konfigurert (mangler ANTHROPIC_API_KEY og GEMINI_API_KEY)."


class MissingKey(Exception):
    """No API key for any page-read model."""


@dataclass
class Usage:
    model: str
    cost: float = 0.0
    fallback: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema → strict JSON schema (every property required, no extra keys, no defaults)."""

    def walk(node: dict) -> None:
        node.pop("default", None)
        node.pop("title", None)
        if "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
        # Keys of 'properties'/'$defs' are field names (Note has a field called 'title'), so only recurse into values
        for key in ("properties", "$defs"):
            for sub in node.get(key, {}).values():
                walk(sub)
        for key in ("items", "anyOf"):
            subs = node.get(key)
            for sub in subs if isinstance(subs, list) else [subs] if subs else []:
                walk(sub)

    schema = model.model_json_schema()
    walk(schema)
    return schema


def claude_request(
    model: str, img: bytes, prompt: str, schema: type[BaseModel], reasoning: str
) -> dict[str, Any]:
    """Messages API arguments for one structured page read; reasoning 'none' turns thinking off."""
    output: dict[str, Any] = {"format": {"type": "json_schema", "schema": strict_schema(schema)}}
    if reasoning != "none":
        output["effort"] = reasoning
    image = {"type": "base64", "media_type": "image/png", "data": base64.b64encode(img).decode()}
    return {
        "model": model,
        "max_tokens": 16000,
        "thinking": {"type": "disabled"} if reasoning == "none" else {"type": "adaptive"},
        "output_config": output,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image", "source": image}, {"type": "text", "text": prompt}],
            }
        ],
    }


def claude_cost(input_tokens: int, output_tokens: int) -> float:
    """USD for one call (output includes thinking)."""
    price_in, price_out = CLAUDE_PRICE[input_tokens > CLAUDE_LONG_PROMPT]
    return (input_tokens * price_in + output_tokens * price_out) / 1e6


class Reader:
    """One shared reader per app: holds the HTTP client and caps concurrent model calls."""

    def __init__(self, concurrency: int = 16) -> None:
        self.claude_key = os.environ.get("ANTHROPIC_API_KEY")
        self.gemini_key = os.environ.get("GEMINI_API_KEY")
        self.slots = asyncio.Semaphore(concurrency)
        self._claude: anthropic.AsyncAnthropic | None = None
        self._gemini: genai.Client | None = None

    def chain(self) -> list[str]:
        out = [MODEL] if self.claude_key else []
        if self.gemini_key and FALLBACK:
            out.append(FALLBACK)
        if not out:
            raise MissingKey(NO_KEY)
        return out

    async def read(
        self, img: bytes, prompt: str, schema: type[BaseModel], reasoning: str = "none"
    ) -> tuple[BaseModel, Usage]:
        """Structured read of one page image; tries each model in the chain ATTEMPTS times."""
        chain = self.chain()
        last: Exception | None = None
        for model in chain:
            for attempt in range(ATTEMPTS):
                try:
                    async with self.slots:
                        parsed, usage = await self._call(model, img, prompt, schema, reasoning)
                    usage.fallback = model != chain[0]
                    return parsed, usage
                except Exception as e:  # rate limits, 5xx, invalid JSON
                    last = e
                    log.warning("page read retry %s (%s): %s", model, attempt, str(e)[:200])
                    await asyncio.sleep(2**attempt * 2)
        raise RuntimeError(f"page read failed: {last}")

    async def _call(
        self, model: str, img: bytes, prompt: str, schema: type[BaseModel], reasoning: str
    ) -> tuple[BaseModel, Usage]:
        if model.startswith("claude"):
            return await self._claude_call(model, img, prompt, schema, reasoning)
        return await self._gemini_call(model, img, prompt, schema)

    async def _claude_call(
        self, model: str, img: bytes, prompt: str, schema: type[BaseModel], reasoning: str
    ) -> tuple[BaseModel, Usage]:
        if self._claude is None:
            # read() retries and falls back itself
            self._claude = anthropic.AsyncAnthropic(
                api_key=self.claude_key, timeout=TIMEOUT, max_retries=0
            )
        try:
            r = await self._claude.messages.create(
                **claude_request(model, img, prompt, schema, reasoning)
            )
        except Exception:
            await usage.claude(None, model, "page-read", "worker", "image", status="error")
            raise
        await usage.claude(r, model, "page-read", "worker", "image")
        # refusal or max_tokens: the text may not match the schema
        if r.stop_reason != "end_turn":
            raise RuntimeError(f"stop_reason {r.stop_reason}")
        text = next(b.text for b in r.content if b.type == "text")
        u = r.usage
        prompt_tokens = (
            u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
        )
        cost = claude_cost(prompt_tokens, u.output_tokens)
        return schema.model_validate_json(text), Usage(model=model, cost=cost)

    async def _gemini_call(
        self, model: str, img: bytes, prompt: str, schema: type[BaseModel]
    ) -> tuple[BaseModel, Usage]:
        if self._gemini is None:
            self._gemini = genai.Client(
                api_key=self.gemini_key, http_options=types.HttpOptions(timeout=int(TIMEOUT * 1000))
            )
        try:
            r = await self._gemini.aio.models.generate_content(
                model=model,
                contents=[types.Part.from_bytes(data=img, mime_type="image/png"), prompt],
                config=types.GenerateContentConfig(
                    temperature=0,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel.MINIMAL
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
        except Exception:
            await usage.gemini(None, model, "page-read", "worker", "image", status="error")
            raise
        await usage.gemini(r, model, "page-read", "worker", "image")
        u = r.usage_metadata
        cost = (
            (u.prompt_token_count or 0) * GEMINI_PRICE[0]
            + (u.candidates_token_count or 0) * GEMINI_PRICE[1]
        ) / 1e6
        return schema.model_validate_json(r.text or ""), Usage(model=model, cost=cost)

    async def aclose(self) -> None:
        if self._claude is not None:
            await self._claude.close()

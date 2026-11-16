"""Report each paid model call to example.org/admin/ai-usage: counts only, no content.

No-op without AI_USAGE_TOKEN and AI_USAGE_OUTBOX. Sending is blocking, so it runs in a thread."""

import asyncio
import os
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from src import usage_client

Kind = Literal["interactive", "worker"]
Modality = Literal["text", "image", "multimodal"]


def claude_event(
    message: Any, model: str, operation: str, kind: Kind, modality: Modality, status: str = "ok"
) -> dict[str, Any]:
    """Dashboard event for one Messages API response (None when the call failed)."""
    u = getattr(message, "usage", None)
    counts: dict[str, int | None] = dict.fromkeys(("input_tokens", "output_tokens", "total_tokens"))
    cached = 0
    if u is not None:
        cached = u.cache_read_input_tokens or 0
        # input_tokens excludes cache reads and writes; the dashboard wants the whole prompt
        prompt = u.input_tokens + cached + (u.cache_creation_input_tokens or 0)
        counts = {
            "input_tokens": prompt,
            "output_tokens": u.output_tokens,
            "total_tokens": prompt + u.output_tokens,
        }
    return {
        "event_id": str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "model": getattr(message, "model", None) or model,
        "provider": "anthropic",
        "kind": kind,
        "operation": operation,
        "status": status,
        **counts,
        "cached_tokens": cached,
        "thinking_tokens": 0,  # Anthropic counts thinking as output
        "tool_tokens": 0,
        "billing": os.getenv("AI_USAGE_BILLING", "unknown"),
        "mode": "standard",
        "modality": modality,
    }


def _enabled() -> bool:
    return bool(os.getenv("AI_USAGE_TOKEN") and os.getenv("AI_USAGE_OUTBOX"))


async def claude(
    message: Any, model: str, operation: str, kind: Kind, modality: Modality, status: str = "ok"
) -> None:
    if _enabled():
        event = claude_event(message, model, operation, kind, modality, status)
        await asyncio.to_thread(usage_client.enqueue, event)


async def gemini(
    response: Any, model: str, operation: str, kind: Kind, modality: Modality, status: str = "ok"
) -> None:
    await asyncio.to_thread(
        usage_client.record,
        response,
        model=model,
        kind=kind,
        operation=operation,
        status=status,
        modality=modality,
    )

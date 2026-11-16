"""JSON API endpoints for service-to-service communication."""

import asyncio
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kladd import run_usage
from kladd.env import require_env

_EXPECTED_API_KEY = require_env("EVALS_API_KEY", dev_default="dev-insecure-api-key")


def _require_api_key(x_api_key: str | None = Header(None)) -> None:
    """Validate X-API-Key header against EVALS_API_KEY env var."""
    if not x_api_key or not secrets.compare_digest(x_api_key, _EXPECTED_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


api_router = APIRouter(prefix="/api", dependencies=[Depends(_require_api_key)])


class GenerateRequest(BaseModel):
    urls: list[str]
    model: str | None = None


@api_router.post("/generate")
async def api_generate(body: GenerateRequest) -> JSONResponse:
    """Generate a draft article from source URLs."""
    from kladd.drafter import draft_article
    from kladd.maalform import detect_nynorsk
    from kladd.quote_check import verify_and_apply
    from kladd.sources import load_and_format
    from kladd.storage import get_draft_sources, save_draft

    if not body.urls:
        raise HTTPException(status_code=400, detail="No URLs provided")

    usage = run_usage.start()
    loaded = await asyncio.to_thread(load_and_format, body.urls)
    nynorsk = detect_nynorsk([s.markdown or "" for s in loaded.parsed])
    article, used_model = await draft_article(
        loaded.formatted,
        loaded.count,
        all_urls=loaded.all_urls,
        model=body.model,
        nynorsk=nynorsk,
    )

    article, quotes_ok, quotes_json, _ = await verify_and_apply(
        article, loaded.parsed, nynorsk=nynorsk
    )

    draft_id = await asyncio.to_thread(
        save_draft,
        article,
        used_model,
        loaded.parsed,
        user_email="api",
        quotes_ok=quotes_ok,
        quotes_json=quotes_json,
    )
    sources = get_draft_sources(draft_id)

    return JSONResponse({
        "draft_id": draft_id,
        "article": article,
        "sources": sources,
        "dropped_sources": [{"source": s, "error": e} for s, e in loaded.dropped],
        "model": used_model,
        "prompt_chars": len(loaded.formatted),
        "quotes_ok": quotes_ok,
        "maalform": "nynorsk" if nynorsk else "bokmål",
        "usage": usage,
    })


@api_router.get("/stats")
async def api_stats() -> JSONResponse:
    """Dashboard stats: draft count, avg time, and recent runs with quote details."""
    from kladd.storage import get_stats

    return JSONResponse(get_stats())


@api_router.get("/drafts/{draft_id}")
async def api_get_draft(draft_id: int) -> JSONResponse:
    """Fetch a single draft by ID."""
    from kladd.storage import get_draft

    draft = get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    return JSONResponse(draft)


@api_router.get("/drafts/{draft_id}/sources")
async def api_get_draft_sources(draft_id: int) -> JSONResponse:
    """Fetch sources for a draft."""
    from kladd.storage import get_draft_sources

    return JSONResponse(get_draft_sources(draft_id))

"""unytt_regnskap — hent årsregnskap (PDF) fra Brønnøysundregistrene."""

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src import archive, db, events, people
from src.accounts import pipeline
from src.accounts.llm import Reader
from src.auth import AuthMiddleware
from src.brreg import BrregClient
from src.chat import gemini_chat
from src.routes import api, pages, user
from src.session_token import session_settings
from src.subsidiaries import gemini_extract

USER_AGENT = "regnskap/0.1 (newsroom tool)"
# httpx drops idle connections after 5 s, so nearly every company open paid a new TLS handshake
# (~150 ms instead of ~40 ms). Brreg was measured reusing them after 240 s idle (2026-10-26).
BRREG_LIMITS = httpx.Limits(
    max_connections=100, max_keepalive_connections=20, keepalive_expiry=120.0
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    events.install_log_handler()
    events.prune()
    events.record("app.start", events.PROCESS, detail={"unfinished_reads": pipeline.unfinished()})
    people.ensure_fresh()  # builds the person search in the background when needed
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0),
        limits=BRREG_LIMITS,
        headers={"User-Agent": USER_AGENT},
    ) as http:
        app.state.brreg = BrregClient(http, on_pdf=archive.keep_pdf)
        watch = (
            asyncio.create_task(pipeline.watch(app.state, api.background))
            if pipeline.WATCH_S
            else None
        )
        yield
        if watch:
            watch.cancel()
        events.record(
            "app.stop",
            events.PROCESS,
            detail={"unfinished_reads": pipeline.unfinished(events.PROCESS)},
        )
    await app.state.reader.aclose()


session_settings()  # bad or missing SESSION_* config fails the deploy, not each request
app = FastAPI(title="unytt-regnskap", lifespan=lifespan)
app.state.asset_version = str(int(time.time()))
# Swappable in tests (no network): PDF → Extraction, and (system, messages) → answer.
app.state.extractor = gemini_extract
app.state.chat_model = gemini_chat
# Page reads for årsregnskap (Claude API, Gemini fallback); swapped for a fake in tests.
app.state.reader = Reader()
app.add_middleware(AuthMiddleware)
app.middleware("http")(events.server_errors)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(api.router)
app.include_router(user.router)
app.include_router(pages.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}

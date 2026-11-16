"""JSON + download endpoints."""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from src import (
    archive,
    chat,
    db,
    events,
    jobs,
    kunngjoringer,
    limits,
    people,
    register,
    subsidiaries,
    xlsx,
)
from src.accounts import history as accounts_history
from src.accounts import payload as accounts_payload
from src.accounts import pipeline as accounts_pipeline
from src.accounts import render as accounts_render
from src.accounts import search as accounts_search
from src.accounts import store as accounts_store
from src.accounts.llm import MissingKey
from src.auth import get_user_email, require_admin
from src.brreg import BrregClient, BrregUnavailable, NotAvailable, parse_key_figures_history
from src.naming import content_disposition, pdf_filename
from src.parsing import SelectionMode, orgnr_error, parse_orgnr_list

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

BRREG_DOWN = "Brønnøysundregistrene svarer ikke akkurat nå. Prøv igjen om litt."
SLOW_OPEN_S = 2.0  # company opens slower than this are recorded (company.slow) with their steps
background: set[asyncio.Task] = set()


def get_client(request: Request) -> BrregClient:
    return request.app.state.brreg


def _valid_orgnr(orgnr: str) -> str:
    if error := orgnr_error(orgnr):
        raise HTTPException(400, error)
    return orgnr


def _people_or_empty(q: str, limit: int = people.LIMIT) -> dict:
    """People are extra: the register being away must not fail the company search."""
    try:
        return people.search(q, limit)
    except (register.Unavailable, sqlite3.Error):
        log.warning("person search failed for %r", q, exc_info=True)
        return {"people": [], "total": 0, "capped": False, "ready": False}


@router.get("/search")
async def search(
    q: str = "",
    with_people: bool = Query(False, alias="people"),
    client: BrregClient = Depends(get_client),
) -> dict:
    """Autocomplete: name search, or direct lookup when q is an org number. With `people=true`
    also people in the shareholder register whose name has words starting with those in q."""
    q = q.strip()
    if len(q) < 2:
        return {"results": [], "people": []}
    found = asyncio.create_task(asyncio.to_thread(_people_or_empty, q)) if with_people else None
    try:
        companies = await client.search(q)
    except (BrregUnavailable, httpx.HTTPError) as e:
        raise HTTPException(502, BRREG_DOWN) from e
    out: dict = {"results": [c.to_dict() for c in companies], "people": []}
    if found:
        hits = await found
        out["people"], out["people_ready"] = hits["people"], hits["ready"]
        out["people_total"], out["people_capped"] = hits["total"], hits["capped"]
    return out


@router.get("/people")
async def search_people(q: str = "") -> dict:
    """Everyone in the shareholder register matching q (the list behind the search's «Alle N
    personer»): anyone named exactly q first, at most people.LIST_MAX."""
    q = q.strip()
    if len(q) < 2:
        return {"people": [], "total": 0, "capped": False, "ready": True}
    return await asyncio.to_thread(_people_or_empty, q, people.LIST_MAX)


async def _roles_or_empty(client: BrregClient, orgnr: str) -> list[dict]:
    """Roles are a nice-to-have: a Brreg hiccup here must not fail the whole company view."""
    try:
        return await client.roles(orgnr)
    except (BrregUnavailable, httpx.HTTPError) as e:
        log.warning("roles lookup failed for %s: %r", orgnr, e)
        return []


async def _owned_by(client: BrregClient, orgnr: str) -> list[dict]:
    """Parents whose subsidiaries note lists orgnr, named from Brreg (all looked up at once)."""

    async def named(o: dict) -> dict:
        try:
            parent = await client.get_company(o["orgnr"])
        except (BrregUnavailable, httpx.HTTPError):
            parent = None
        return {**o, "name": parent.name if parent else None}

    return await asyncio.gather(*(named(o) for o in db.owned_by(orgnr)))


async def _timed[T](steps: dict[str, float], step: str, aw: Awaitable[T]) -> T:
    """Await aw, noting its seconds under steps[step]."""
    start = time.perf_counter()
    try:
        return await aw
    finally:
        steps[step] = round(time.perf_counter() - start, 3)


async def company_payload(request: Request, orgnr: str, email: str, record: bool) -> dict:
    """Everything the company view (and the chat context) needs for one orgnr."""
    client = get_client(request)
    start = time.perf_counter()
    steps: dict[str, float] = {}
    # All Brreg lookups at once, so a cold open waits one round trip rather than one per lookup
    try:
        info, years, live_rows, roles, owned_by = await asyncio.gather(
            _timed(steps, "enhet", client.get_company(orgnr)),
            _timed(steps, "aar", client.list_years(orgnr)),
            _timed(steps, "regnskap", client.figure_rows(orgnr)),
            _timed(steps, "roller", _roles_or_empty(client, orgnr)),
            _timed(steps, "eiere", _owned_by(client, orgnr)),
        )
    except (BrregUnavailable, httpx.HTTPError) as e:
        raise HTTPException(502, BRREG_DOWN) from e
    if info is None:
        raise HTTPException(404, f"Fant ikke {orgnr} i Enhetsregisteret.")
    company = info.to_dict()
    company["roles"] = roles
    # Kept so years Brreg stops serving still show (marked archived). After the roles lookup: raw() has its JSON
    rows = await _timed(
        steps,
        "arkiv",
        asyncio.to_thread(archive.keep_company, orgnr, live_rows, client.raw(orgnr)),
    )
    history = parse_key_figures_history(rows)
    group = parse_key_figures_history(rows, "KONSERN")
    figures = history[0] if history else None
    if record:
        db.touch_recent(email, orgnr, info.name)
        _maybe_autostart(request, orgnr, years, figures)
    out = {
        "company": company,
        "years": years,
        "key_figures": figures,
        "key_figures_history": history,
        "key_figures_konsern_history": group,
        "favourite": db.is_favourite(email, orgnr),
        "collections": db.collections_containing(email, orgnr),
        "ownership": {
            "owned_by": owned_by,
            "subsidiaries": subsidiaries.status(orgnr),
        },
        "accounts": accounts_store.status_rows(orgnr),
    }
    if (total := time.perf_counter() - start) > SLOW_OPEN_S:
        events.record(
            "company.slow",
            f"{total:.1f} s",
            level="warning",
            orgnr=orgnr,
            detail={"steps": steps, "owners": len(owned_by)},
        )
    return out


def _maybe_autostart(request: Request, orgnr: str, years: list[int], figures: dict | None) -> None:
    """Parent companies get their subsidiaries note read once per newest PDF year."""
    if not years or not (figures and figures.get("morselskap")):
        return
    if db.get_extraction(orgnr, max(years)):
        return
    subsidiaries.start(
        get_client(request), orgnr, max(years), request.app.state.extractor, background
    )


@router.get("/company/{orgnr}")
async def company(orgnr: str, request: Request, email: str = Depends(get_user_email)) -> dict:
    """Company facts, roles, PDF years, key figures, user's favourite/collections, ownership."""
    return await company_payload(request, _valid_orgnr(orgnr), email, record=True)


@router.get("/company/{orgnr}/subsidiaries")
async def get_subsidiaries(orgnr: str) -> dict:
    return subsidiaries.status(_valid_orgnr(orgnr))


@router.post("/company/{orgnr}/subsidiaries", status_code=202)
async def start_subsidiaries(
    orgnr: str,
    request: Request,
    force: bool = False,
    client: BrregClient = Depends(get_client),
    email: str = Depends(get_user_email),
) -> dict:
    """Read the subsidiaries note from the newest PDF in the background."""
    orgnr = _valid_orgnr(orgnr)
    current = subsidiaries.status(orgnr)
    if current["status"] == "running" or (current["status"] in ("ok", "none") and not force):
        return current
    if current["status"] in ("ok", "none"):
        limits.take(
            limits.SUBSIDIARY_RERUNS,
            email,
            1,
            "Du har lest datterselskaper på nytt mange ganger i dag. Prøv igjen i morgen.",
        )
    try:
        years = await client.list_years(orgnr)
    except (BrregUnavailable, httpx.HTTPError) as e:
        raise HTTPException(502, BRREG_DOWN) from e
    if not years:
        raise HTTPException(404, "Brreg har ingen årsregnskap (PDF) for dette selskapet.")
    subsidiaries.start(client, orgnr, max(years), request.app.state.extractor, background)
    return subsidiaries.status(orgnr)


class ReadRequest(BaseModel):
    years: list[int] = Field(min_length=1, max_length=60)


@router.get("/company/{orgnr}/accounts")
async def accounts_status(orgnr: str) -> dict:
    """Which years have been read (or are being read) into figures, notes and text."""
    return {"years": accounts_store.status_rows(_valid_orgnr(orgnr))}


@router.post("/company/{orgnr}/accounts", status_code=202)
async def read_accounts(
    orgnr: str,
    req: ReadRequest,
    request: Request,
    client: BrregClient = Depends(get_client),
    email: str = Depends(get_user_email),
) -> dict:
    """Read the chosen years' PDFs page by page in the background, newest first."""
    orgnr = _valid_orgnr(orgnr)
    events.record("read.requested", email, orgnr=orgnr, detail={"years": req.years})
    try:
        info, available = await asyncio.gather(client.get_company(orgnr), client.list_years(orgnr))
    except (BrregUnavailable, httpx.HTTPError) as e:
        raise HTTPException(502, BRREG_DOWN) from e
    years = [y for y in req.years if y in available]
    if not years:
        raise HTTPException(400, "Brreg har ikke årsregnskap (PDF) for de valgte årene.")
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if accounts_store.spent_since(midnight.isoformat()) >= limits.READ_BUDGET_USD:
        raise HTTPException(429, "Dagens budsjett for innlesing er brukt opp. Prøv igjen i morgen.")
    limits.take(
        limits.READ_YEARS,
        email,
        len(years),
        f"Grensen er {limits.READ_YEARS.limit} innleste år per døgn per bruker. Prøv igjen senere.",
    )
    try:
        queued = accounts_pipeline.start(
            client,
            request.app.state.reader,
            orgnr,
            info.name if info else orgnr,
            years,
            background,
        )
    except MissingKey as e:
        raise HTTPException(503, str(e)) from e
    if skipped := sorted(set(req.years) - set(queued)):
        events.record(
            "read.skipped",
            "ikke i Brregs liste eller allerede i gang",
            orgnr=orgnr,
            detail={"years": skipped},
        )
    return {"queued": queued, "years": accounts_store.status_rows(orgnr)}


DIAG_DAYS = 7


@router.get("/diagnostics")
async def diagnostics(
    orgnr: str | None = None,
    year: int | None = None,
    since: str | None = None,
    level: str | None = None,
    kind: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    _admin: str = Depends(require_admin),
) -> dict:
    """Admin only (it shows who read which company). What happened lately, for debugging: events (newest first; level=warning for problems only, kind is a
    prefix like 'read') and reads started or finished in the last DIAG_DAYS days."""
    since_reads = (datetime.now(UTC) - timedelta(days=DIAG_DAYS)).isoformat()
    return {
        "release": events.RELEASE,
        "process": events.PROCESS,
        "now": db.now_iso(),
        "reads": [
            r for r in accounts_store.recent_reads(since_reads) if not orgnr or r["orgnr"] == orgnr
        ],
        "events": events.recent(orgnr, year, since, level, kind, limit),
    }


class ClientLog(BaseModel):
    kind: Literal["js", "promise", "network", "api", "shown"]
    message: str = Field(max_length=2000)
    stack: str | None = Field(None, max_length=8000)
    view: str | None = Field(None, max_length=40)
    url: str | None = Field(None, max_length=1000)
    orgnr: str | None = Field(None, max_length=9)


# Deep-link params that name no private person; personer, eier, eierstruktur and f are dropped before logging.
LOGGED_PARAMS = {"orgnr", "regnskap", "aar", "eierkart", "modus", "samling", "side"}


def _loggable_url(url: str | None) -> str | None:
    """The page's path and its non-personal query params."""
    if not url:
        return url
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k in LOGGED_PARAMS])
    return parts.path + (f"?{query}" if query else "")


@router.post("/client-log", status_code=204)
async def client_log(
    body: ClientLog, request: Request, email: str = Depends(get_user_email)
) -> None:
    """An error the browser hit (JS exception, failed API call, error shown to the user).
    Over the user's limit the event is dropped without telling the browser."""
    if not limits.CLIENT_LOG.take(email.lower()):
        return
    events.record(
        f"client.{body.kind}",
        body.message,
        level="error",
        orgnr=body.orgnr,
        detail={
            "user": email,
            "view": body.view,
            "url": _loggable_url(body.url),
            "stack": body.stack,
            "agent": request.headers.get("user-agent", "")[:300],
        },
    )


@router.get("/company/{orgnr}/accounts/search")
async def account_search(orgnr: str, q: str = "") -> dict:
    """Text passages and figures in every read year of this company that match q."""
    orgnr = _valid_orgnr(orgnr)
    q = q.strip()
    if len(q) < 2:
        return {"q": q, "text": [], "figures": []}
    text, figures = await asyncio.gather(
        asyncio.to_thread(accounts_search.text_hits, orgnr, q),
        asyncio.to_thread(accounts_search.figure_hits, orgnr, q),
    )
    return {"q": q, "text": text, "figures": figures}


@router.get("/company/{orgnr}/accounts/figures")
async def account_figures(orgnr: str, client: BrregClient = Depends(get_client)) -> dict:
    """Key figures for read years Brreg lacks as data, each figure with its sources and verdict, per scope."""
    orgnr = _valid_orgnr(orgnr)
    try:
        live, years = await asyncio.gather(client.figure_rows(orgnr), client.list_years(orgnr))
    except (BrregUnavailable, httpx.HTTPError):
        live, years = [], []
    rows = await asyncio.to_thread(archive.regnskap_rows, orgnr, live)
    brreg = {s: parse_key_figures_history(rows, s.upper()) for s in ("selskap", "konsern")}
    return await asyncio.to_thread(accounts_history.figures, orgnr, brreg, years)


@router.get("/company/{orgnr}/accounts/{year}")
async def account_detail(orgnr: str, year: int, client: BrregClient = Depends(get_client)) -> dict:
    """Statements, notes, text and remaining checks of one read year. Checked against Brreg's figures for the
    year where Brreg has them; last year's column is compared with last year's figures (Brreg's, else our read)."""
    orgnr = _valid_orgnr(orgnr)
    refs = await accounts_pipeline.brreg_refs(client, orgnr, year)
    prior = await accounts_pipeline.brreg_refs(client, orgnr, year - 1)
    for scope in ("selskap", "konsern"):
        if scope not in prior and (ours := accounts_store.statement_totals(orgnr, year - 1, scope)):
            prior[scope] = ours
    out = await asyncio.to_thread(accounts_payload.detail, orgnr, year, refs, prior)
    if out is None:
        raise HTTPException(404, f"Årsregnskapet for {year} er ikke lest inn.")
    return out


def _stored(orgnr: str, year: int) -> dict:
    f = accounts_store.stored_pdf(_valid_orgnr(orgnr), year)
    if not f or not f["abs_path"].exists():
        raise HTTPException(404, f"Årsregnskapet for {year} er ikke lest inn.")
    return f


@router.get("/company/{orgnr}/accounts/{year}/pdf")
async def account_pdf(orgnr: str, year: int) -> FileResponse:
    """The exact PDF the figures were read from, shown inline so #page=N opens at the page."""
    f = _stored(orgnr, year)
    return FileResponse(
        f["abs_path"], media_type="application/pdf", headers={"Content-Disposition": "inline"}
    )


@router.get("/company/{orgnr}/accounts/{year}/pages/{page}")
async def account_page(orgnr: str, year: int, page: int) -> Response:
    """One page of the stored PDF as WebP for the scan viewer."""
    f = _stored(orgnr, year)
    if not 1 <= page <= f["n_pages"]:
        raise HTTPException(404, "Siden finnes ikke.")
    path = accounts_store.page_cache_dir(f["sha256"]) / f"v{page}.webp"
    img = await accounts_render.cached(
        f["abs_path"].read_bytes(), page, path, accounts_render.view_webp
    )
    return Response(
        img, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"}
    )


REGISTER_DOWN = "Aksjonærregisteret er ikke tilgjengelig akkurat nå."


@router.get("/company/{orgnr}/shareholders")
async def get_shareholders(orgnr: str, year: int | None = None) -> dict:
    """Owners, chain upwards, holdings and change tags for one register year (default latest)."""
    orgnr = _valid_orgnr(orgnr)
    try:
        snap = await asyncio.to_thread(register.snapshot, orgnr, year)
    except register.Unavailable as e:
        raise HTTPException(503, REGISTER_DOWN) from e
    saved = db.structure_saved(orgnr, snap["year"], register.STRUCTURE_VERSION)
    return {**snap, "structure_saved": saved}


async def _register_year(year: int | None) -> int:
    """`year` if the register has it, else its newest year."""
    try:
        years = await asyncio.to_thread(register.years)
    except register.Unavailable as e:
        raise HTTPException(503, REGISTER_DOWN) from e
    if not years:
        raise HTTPException(503, REGISTER_DOWN)
    return year if year in years else years[-1]


@router.get("/company/{orgnr}/structure")
async def get_structure(orgnr: str, year: int | None = None) -> dict:
    """The saved eierkart for one register year; 404 until it has been made (POST)."""
    orgnr = _valid_orgnr(orgnr)
    year = await _register_year(year)
    saved = db.get_structure(orgnr, year, register.STRUCTURE_VERSION)
    if saved is None:
        raise HTTPException(404, "Selskapsstrukturen er ikke laget ennå.")
    return saved


@router.post("/company/{orgnr}/structure")
async def make_structure(orgnr: str, year: int | None = None) -> dict:
    """Build the eierkart from the register and save it (replacing any saved one)."""
    orgnr = _valid_orgnr(orgnr)
    try:
        data = await asyncio.to_thread(register.structure, orgnr, year)
    except register.Unavailable as e:
        raise HTTPException(503, REGISTER_DOWN) from e
    if not data["found"]:
        raise HTTPException(404, "Selskapet har ingen eiere eller aksjer i aksjonærregisteret.")
    created = db.save_structure(orgnr, data["year"], register.STRUCTURE_VERSION, data)
    return {**data, "created_at": created}


@router.get("/company/{orgnr}/holdings-tree")
async def get_holdings_tree(orgnr: str, year: int | None = None) -> dict:
    """What one company holds, followed down: the map opens a stake under 20 % with this."""
    orgnr = _valid_orgnr(orgnr)
    year = await _register_year(year)
    try:
        return await asyncio.to_thread(register.holdings_tree, orgnr, year)
    except register.Unavailable as e:
        raise HTTPException(503, REGISTER_DOWN) from e


_kunngjoring_locks: dict[str, asyncio.Lock] = {}


def _kunngjoring_payload(orgnr: str, stale: bool) -> dict:
    items = db.list_kunngjoringer(orgnr)
    for it in items:
        it["url"] = kunngjoringer.one_url(it["kid"], orgnr)
    return {
        "items": items,
        "unread": sum(1 for it in items if not it["read"]),
        "fetched_at": db.kunngjoringer_fetched(orgnr),
        "stale": stale,
        "source": kunngjoringer.list_url(orgnr),
    }


@router.get("/company/{orgnr}/kunngjoringer")
async def get_kunngjoringer(
    orgnr: str,
    batch: int = Query(kunngjoringer.BATCH, ge=0, le=kunngjoringer.BATCH),
    client: BrregClient = Depends(get_client),
) -> dict:
    """Every kunngjøring Brreg has published for the company, newest first. The list is fetched
    again once a day; each call reads up to `batch` more announcements, newest first, and keeps
    them. The page asks for a few at a time so it can draw as they come."""
    orgnr = _valid_orgnr(orgnr)
    stale = False
    async with _kunngjoring_locks.setdefault(orgnr, asyncio.Lock()):
        try:
            if not db.kunngjoringer_fresh(orgnr):
                items = await kunngjoringer.fetch_list(client, orgnr)
                if items is None:
                    raise HTTPException(404, "Brreg har ingen kunngjøringer for dette org.nr.")
                await asyncio.to_thread(db.save_kunngjoring_list, orgnr, items)
            if batch and (unread := db.kunngjoring_unread(orgnr, batch)):
                details = await kunngjoringer.fetch_details(client, orgnr, unread)
                await asyncio.to_thread(db.save_kunngjoring_details, details)
        except (BrregUnavailable, httpx.HTTPError):
            if not db.kunngjoringer_fetched(orgnr):
                raise HTTPException(503, BRREG_DOWN) from None
            stale = True
    return _kunngjoring_payload(orgnr, stale)


@router.get("/company/{orgnr}/kunngjoringer/{kid}")
async def get_kunngjoring(orgnr: str, kid: str, client: BrregClient = Depends(get_client)) -> dict:
    """One listed announcement, read now if it hasn't been yet."""
    orgnr = _valid_orgnr(orgnr)
    if db.kunngjoring_orgnr(kid) != orgnr:
        raise HTTPException(404, "Ukjent kunngjøring.")
    if kid in db.kunngjoring_unread(orgnr, 10_000):
        try:
            details = await kunngjoringer.fetch_details(client, orgnr, [kid])
        except (BrregUnavailable, httpx.HTTPError):
            raise HTTPException(503, BRREG_DOWN) from None
        await asyncio.to_thread(db.save_kunngjoring_details, details)
    return next(it for it in _kunngjoring_payload(orgnr, False)["items"] if it["kid"] == kid)


@router.get("/company/{orgnr}/shareholders/history")
async def get_shareholder_history(orgnr: str) -> dict:
    """Holders × years and the list of ownership changes."""
    try:
        return await asyncio.to_thread(register.history, _valid_orgnr(orgnr))
    except register.Unavailable as e:
        raise HTTPException(503, REGISTER_DOWN) from e


@router.get("/person")
async def get_person(
    name: str = Query(min_length=1, max_length=200),
    born: int | None = Query(None, ge=1800, le=2100),
    year: int | None = None,
) -> dict:
    """Holdings of one person in the shareholder register, identified by name + birth year."""
    try:
        d = await asyncio.to_thread(register.person, name.strip(), born, year)
    except register.Unavailable as e:
        raise HTTPException(503, REGISTER_DOWN) from e
    key = register.person_key(d["name"], born)
    return {**d, "structure_saved": db.structure_saved(key, d["year"], register.STRUCTURE_VERSION)}


PersonName = Query(min_length=1, max_length=200)
PersonBorn = Query(None, ge=1800, le=2100)


@router.get("/person/structure")
async def get_person_structure(
    name: str = PersonName, born: int | None = PersonBorn, year: int | None = None
) -> dict:
    """The saved eierstruktur of one person; 404 until it has been made (POST)."""
    year = await _register_year(year)
    saved = db.get_structure(
        register.person_key(name.strip(), born), year, register.STRUCTURE_VERSION
    )
    if saved is None:
        raise HTTPException(404, "Eierstrukturen er ikke laget ennå.")
    return saved


@router.post("/person/structure")
async def make_person_structure(
    name: str = PersonName, born: int | None = PersonBorn, year: int | None = None
) -> dict:
    """Build one person's eierstruktur from the register and save it."""
    try:
        data = await asyncio.to_thread(register.person_structure, name.strip(), born, year)
    except register.Unavailable as e:
        raise HTTPException(503, REGISTER_DOWN) from e
    if not data["found"]:
        raise HTTPException(404, "Personen har ingen aksjer i aksjonærregisteret.")
    created = db.save_structure(data["orgnr"], data["year"], register.STRUCTURE_VERSION, data)
    return {**data, "created_at": created}


def _register_for_chat(orgnr: str) -> str | None:
    """Register block for the chat context; None when the register isn't available."""
    try:
        snap = register.snapshot(orgnr)
        hist = register.history(orgnr) if snap["found"] and snap["n_changes"] else None
    except register.Unavailable:
        return None
    return chat.register_context(orgnr, snap, hist)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    orgnr: str | None = None
    view: str | None = None
    collection_id: int | None = None


@router.post("/chat")
async def chat_answer(
    body: ChatRequest, request: Request, email: str = Depends(get_user_email)
) -> dict:
    """Answer a question about the open company / the user's lists."""
    messages = chat.trim([m.model_dump() for m in body.messages])
    if not messages:
        raise HTTPException(400, "Siste melding må være fra brukeren.")
    limits.take(limits.CHAT, email, 1, "Mange spørsmål på kort tid. Vent litt og prøv igjen.")
    company_data = register_text = excerpts = None
    if body.orgnr:
        orgnr = _valid_orgnr(body.orgnr)
        company_data = await company_payload(request, orgnr, email, record=False)
        register_text = await asyncio.to_thread(_register_for_chat, orgnr)
        # Passages from the company's read filings that match the question
        excerpts = await asyncio.to_thread(
            accounts_search.chat_excerpts, orgnr, messages[-1]["content"]
        )
    open_collection = db.get_collection(email, body.collection_id) if body.collection_id else None
    system = chat.build_system(
        company_data,
        db.list_favourites(email),
        db.list_collections(email),
        open_collection,
        register_text,
        excerpts,
    )
    try:
        text = await request.app.state.chat_model(system, messages)
    except subsidiaries.MissingKey as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:
        log.exception("chat failed")
        raise HTTPException(502, "AI-en svarte ikke. Prøv igjen.") from e
    if not text:
        raise HTTPException(502, "AI-en ga et tomt svar. Prøv å spørre annerledes.")
    return {"text": text}


@router.get("/pdf/{orgnr}/{year}")
async def pdf(orgnr: str, year: int, client: BrregClient = Depends(get_client)) -> Response:
    """One annual-accounts PDF, renamed to <COMPANY>_<year>.pdf."""
    orgnr = _valid_orgnr(orgnr)
    try:
        info = await client.get_company(orgnr)
        data = await client.fetch_pdf(orgnr, year)
    except NotAvailable as e:
        raise HTTPException(404, str(e)) from e
    except (BrregUnavailable, httpx.HTTPError) as e:
        raise HTTPException(502, BRREG_DOWN) from e
    name = info.name if info else orgnr
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(pdf_filename(name, year))},
    )


class JobRequest(BaseModel):
    orgnrs: str | list[str]
    mode: SelectionMode = "all"
    # Bounded: _resolve records one failure per requested year Brreg lacks, per company.
    years: list[Annotated[int, Field(ge=1900, le=2100)]] | None = Field(None, max_length=200)
    last_n: int | None = Field(None, ge=1, le=100)
    from_year: int | None = None
    to_year: int | None = None
    batch: bool = False


async def _run(client: BrregClient, job: jobs.Job) -> None:
    try:
        await jobs.run_job(client, job)
    except Exception:
        log.exception("job %s crashed", job.id)
        job.phase, job.error = "failed", "Noe gikk galt under nedlastingen. Prøv igjen."


@router.post("/jobs")
async def create_job(
    body: JobRequest,
    client: BrregClient = Depends(get_client),
    email: str = Depends(get_user_email),
) -> dict:
    """Start a ZIP job. Invalid lines are reported back immediately as failures."""
    text = body.orgnrs if isinstance(body.orgnrs, str) else "\n".join(body.orgnrs)
    # Every invalid token becomes a stored Failure, so the input itself is capped.
    if len(text) > jobs.MAX_ORGNR_TEXT:
        raise HTTPException(
            400, f"For lang liste. Maks {jobs.MAX_ORGNRS} selskaper per nedlasting."
        )
    parsed = parse_orgnr_list(text)
    if not parsed.orgnrs:
        raise HTTPException(400, "Fant ingen gyldige organisasjonsnummer.")
    if len(parsed.orgnrs) > jobs.MAX_ORGNRS:
        raise HTTPException(
            400, f"Maks {jobs.MAX_ORGNRS} selskaper per nedlasting (fikk {len(parsed.orgnrs)})."
        )
    if body.mode == "years" and not body.years:
        raise HTTPException(400, "Velg minst ett år.")
    selection = jobs.Selection(body.mode, body.years, body.last_n, body.from_year, body.to_year)
    try:
        selection.apply([])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if jobs.active(email) >= jobs.MAX_ACTIVE_PER_USER:
        raise HTTPException(
            429,
            f"Du har allerede {jobs.MAX_ACTIVE_PER_USER} nedlastinger i gang. Vent til en er ferdig.",
        )
    if jobs.active() >= jobs.MAX_ACTIVE:
        raise HTTPException(429, "Mange nedlastinger i gang akkurat nå. Prøv igjen om litt.")

    job = jobs.create_job(email, parsed.orgnrs, selection, batch=body.batch)
    job.failures.extend(jobs.invalid_failures(parsed.invalid))
    task = asyncio.create_task(_run(client, job))
    background.add(task)
    task.add_done_callback(background.discard)
    return job.to_dict()


def _own_job(job_id: str, email: str) -> jobs.Job:
    job = jobs.JOBS.get(job_id)
    if not job or job.owner != email:
        raise HTTPException(404, "Fant ikke nedlastingen (den kan ha utløpt).")
    return job


@router.get("/jobs/{job_id}")
async def job_status(job_id: str, email: str = Depends(get_user_email)) -> dict:
    return _own_job(job_id, email).to_dict()


@router.get("/jobs/{job_id}/download")
async def job_download(job_id: str, email: str = Depends(get_user_email)) -> FileResponse:
    job = _own_job(job_id, email)
    if job.phase != "done" or not job.path or not job.path.exists():
        raise HTTPException(409, "ZIP-filen er ikke klar.")
    return FileResponse(
        job.path,
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(job.filename or "arsregnskap.zip")},
    )


class XlsxSheet(BaseModel):
    name: str = Field(min_length=1, max_length=31, pattern=r"^[^\[\]:*?/\\]+$")
    rows: list[list[str | int | float | None]] = Field(min_length=1, max_length=200)
    bold: list[int] = Field(default=[], max_length=200)
    indent: list[int] = Field(default=[], max_length=200)


class XlsxRequest(BaseModel):
    filename: str = Field(max_length=120, pattern=r"^[\w\-]+\.xlsx$")
    sheets: list[XlsxSheet] = Field(min_length=1, max_length=5)


@router.post("/xlsx")
def make_xlsx(req: XlsxRequest, _email: str = Depends(get_user_email)) -> Response:
    """Rows the page already shows → .xlsx (one worksheet per sheet). No lookups."""
    if any(len(row) > 40 for sheet in req.sheets for row in sheet.rows):
        raise HTTPException(422, "For mange kolonner.")
    return Response(
        xlsx.build_workbook([s.model_dump() for s in req.sheets]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(req.filename)},
    )

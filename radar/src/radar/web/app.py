"""FastAPI app: the ranked education feed."""

from __future__ import annotations

import hmac
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

import uvicorn
from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .. import config, storage
from ..source import ScraperSource
from ..text import article_html, standfirst
from . import mute, queries, searchq
from .auth import AuthMiddleware, auth_disabled, current_user, require_stats_token
from .queries import HL_CLOSE, HL_OPEN, PER_PAGE, SOURCE_LABELS
from .searchq import (
    COVERAGE_LABELS_NO as COVERAGE_LABELS,
)
from .searchq import (
    KIND_LABELS_NO as KIND_LABELS,
)
from .searchq import (
    STAGE_LABELS,
)
from .session_token import session_settings

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

MONTHS_NO = ["", "januar", "februar", "mars", "april", "mai", "juni",
             "juli", "august", "september", "oktober", "november", "desember"]

# A tick fires every two hours. Three of them missed and the radar is not a
# radar any more, whatever the process says about itself.
STALE_TICK_HOURS = 7


def to_oslo(value: str | None) -> str | None:
    """Rewrite an ISO timestamp into Norwegian local time.

    Timestamps are stored in UTC, so the wall clock in the string is not the one
    a Norwegian reader expects — an hour off in winter, two in summer. Values
    without an offset (and bare dates) are passed through: nothing in this app
    writes them, and guessing a zone for them would be worse than leaving them.
    """
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone(config.OSLO).isoformat()


def fmt_time(value: str | None) -> str:
    """ISO timestamp → HH:MM, Norwegian local time."""
    value = to_oslo(value)
    if not value or len(value) < 16:
        return ""
    return value[11:16]


def fmt_datestamp(value: str | None) -> str:
    value = to_oslo(value)
    if not value or len(value) < 10:
        return ""
    return f"{value[8:10]}.{value[5:7]}.{value[2:4]}"


def fmt_date_long(value: str | None) -> str:
    """ISO date → "6. august 2026"."""
    value = to_oslo(value)
    if not value or len(value) < 10:
        return ""
    try:
        month = int(value[5:7])
        if not 1 <= month <= 12:  # month 0 would index the empty leading entry
            return value[:10]
        return f"{int(value[8:10])}. {MONTHS_NO[month]} {value[0:4]}"
    except (ValueError, IndexError):
        return value[:10]


def highlighted(value: str | None) -> Markup:
    """An FTS5 snippet as safe HTML, with <mark> around the hits.

    Everything is escaped first and only our own two sentinels become tags, so a
    headline containing a script tag is inert. This is why the SQL asks FTS5 for
    char(2)/char(3) rather than for '<mark>' directly — asking for the tags and
    then reaching for |safe would ship third-party HTML straight to the page.
    """
    if not value:
        return Markup("")
    return Markup(
        str(escape(value)).replace(HL_OPEN, "<mark>").replace(HL_CLOSE, "</mark>")
    )


def saker(n: int | None) -> str:
    """«1 sak», «2 saker». Norwegian does not add -er at one."""
    return "sak" if n == 1 else "saker"


def other_sources_label(n: int | None) -> str:
    """How many other outlets carried the same story. Empty when nobody else did.

    The feed only ever states the count — which outlets they are is the story
    page's job, on the timeline.
    """
    if not n or n < 1:
        return ""
    return "Én annen kilde" if n == 1 else f"{n} andre kilder"


# The scraper's paywall flag has several sources, and the publisher's own
# schema.org declaration is the noisiest of them: frifagbevegelse.no marks every
# article gated and then serves the whole text to anyone who asks. Rather than
# repeat the claim, check the only part of it that matters — that the reader is
# looking at a taste of the article rather than the article.
#
# Measured across the flagged articles in the radar: the sources whose flag is
# right serve teasers that top out just under 2 900 characters, so a body past
# 3 000 is a whole story.
FULL_ARTICLE_CHARS = 3000


def behind_wall(item) -> bool:
    """True when the reader really is being shown a teaser."""
    return bool(item.get("paywalled")) and (item.get("body_chars") or 0) < FULL_ARTICLE_CHARS


def coverage_mark(item) -> dict | None:
    """The «har vi den?» badge for one row, or None when we have nothing.

    Two ways of knowing. A link from one of our own articles is proof and is
    labelled as such; a model verdict is a claim about the event rather than
    about this URL, so it gets the softer label and a tooltip that explains
    itself. Both name an article the reader can go and check — and carry its
    URL, because a badge whose evidence lives only in a `title` attribute cannot
    be checked at all on a phone.
    """
    status = item.get("coverage_status")
    if status == "sitert":
        return {
            "level": "sitert",
            "label": "Sitert av oss",
            "url": item.get("internal_url"),
            "title": _our_article("Vi har sitert denne saken", item),
        }
    if status == "dekket":
        return {
            "level": "dekket",
            "label": "Vi har saken",
            "url": item.get("internal_url"),
            "title": _our_article("Ser ut til å være dekket hos oss", item),
        }
    # Covered and still worth reading. The label says what the desk should do
    # about it, which is the opposite of what the other two say.
    if status == "oppfolging":
        return {
            "level": "oppfolging",
            "label": "Ny reaksjon",
            "url": item.get("internal_url"),
            "whats_new": item.get("coverage_whats_new"),
            "title": item.get("coverage_whats_new")
            or _our_article("Vi har hendelsen, men denne saken er ny", item),
        }
    return None


def _our_article(lead: str, item) -> str:
    """Tooltip: which of our articles, when, and why the model thought so."""
    parts = [lead]
    if item.get("internal_title"):
        parts.append(f": «{item['internal_title']}»")
    if item.get("internal_day"):
        parts.append(f" ({fmt_date_long(item['internal_day'])})")
    if item.get("coverage_rationale"):
        parts.append(f" — {item['coverage_rationale']}")
    return "".join(parts)


def qs_value(value: str | None) -> str:
    """Percent-encode a query-string value. Article ids are whole URLs."""
    return quote(str(value or ""), safe="")


def truncate_chars(value: str | None, n: int = 240) -> str:
    if not value:
        return ""
    s = str(value).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def source_label(value: str | None) -> str:
    """Stored source name → the name readers know it by."""
    return SOURCE_LABELS.get(value or "", value or "")


def domain(url: str | None) -> str:
    if not url:
        return ""
    return url.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")


def sitatkladd_link(url: str | None) -> str:
    """Hand-off to sitatkladd, which starts drafting on the URL as it loads."""
    return f"{config.SITATKLADD_URL}/?{urlencode({'url': url or ''})}"


class CachedStatic(StaticFiles):
    """Static files with a far-future cache, since every URL is versioned."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("cache-control", "public, max-age=31536000, immutable")
        return response


class SecurityHeaders:
    """Headers every response gets. Pure ASGI, so it also covers static files.

    The story page renders HTML derived from scraped sources, so a CSP is the
    cheap mitigation for anything that gets through `text.article_html`. And
    the referrer policy: every outbound click on this page goes to a publisher,
    and without it each one is told the internal URL — filters, search terms
    and all — that the reader was looking at.

    `same-origin`, not `no-referrer`. The Fetch spec has a browser send
    `Origin: null` on every POST under `no-referrer`, same-origin ones included,
    so with the stricter value not one of our own forms passed `_same_origin`
    in Chrome. `same-origin` keeps the URL from publishers just the same.
    """

    CSP = (
        "default-src 'self'; "
        "img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    )
    HEADERS = {
        b"content-security-policy": CSP.encode(),
        b"x-content-type-options": b"nosniff",
        b"referrer-policy": b"same-origin",
        b"x-frame-options": b"DENY",
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                headers.extend(
                    (k, v) for k, v in self.HEADERS.items() if k not in present
                )
            await send(message)

        await self.app(scope, receive, wrapped)


def _same_origin(request: Request) -> bool:
    """Whether a state-changing request came from our own page.

    Everything else here is a GET, so this is the only place cross-site request
    forgery is even possible — and the session cookie is shared across
    .example.org, so a forged POST from any app on the domain would carry it.
    """
    own = _own_origin(request)
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/") == own
    referer = request.headers.get("referer")
    return bool(referer and referer.startswith(own + "/"))


def _own_origin(request: Request) -> str:
    """scheme://host as the browser sees it.

    A proxy that rewrites Host (portless does; Dokku's nginx does not) leaves the
    original in X-Forwarded-Host. A browser cannot set that header on a
    cross-site form post, so trusting it costs nothing against the forgery this
    check exists for.
    """
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    return f"{request.url.scheme}://{host}"


def create_app() -> FastAPI:
    app = FastAPI(title="Utdanningsradar")

    app.add_middleware(GZipMiddleware, minimum_size=800)
    if not auth_disabled():
        session_settings()  # bad or missing SESSION_* config fails the deploy, not each request
        app.add_middleware(AuthMiddleware)

    # Every reference carries ?v=<mtime>, so the bytes behind a URL never change
    # and a year is safe. Without this the 400 KB of fonts is revalidated on
    # every page load.
    app.mount("/static", CachedStatic(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["time"] = fmt_time
    templates.env.filters["datestamp"] = fmt_datestamp
    templates.env.filters["date_long"] = fmt_date_long
    templates.env.filters["truncate_chars"] = truncate_chars
    templates.env.filters["domain"] = domain
    templates.env.filters["source_label"] = source_label
    templates.env.filters["qs_value"] = qs_value
    templates.env.filters["other_sources"] = other_sources_label
    templates.env.filters["coverage_mark"] = coverage_mark
    templates.env.filters["behind_wall"] = behind_wall
    templates.env.filters["sitatkladd"] = sitatkladd_link
    templates.env.filters["highlighted"] = highlighted
    templates.env.filters["saker"] = saker

    def style_version() -> str:
        try:
            newest = max(
                p.stat().st_mtime for p in STATIC_DIR.glob("*") if p.is_file()
            )
            return str(int(newest))
        except (OSError, ValueError):
            return "0"

    def parse_q(
        conn, raw: str | None, page: int, sources: list[str], hidden: tuple[str, ...] = ()
    ) -> searchq.Query:
        return searchq.parse(
            raw,
            sources=frozenset(sources),
            source_aliases={v: k for k, v in SOURCE_LABELS.items()},
            page=page,
            hidden=hidden,
        )

    def shell_context(request: Request, conn, q: searchq.Query) -> dict:
        """Everything base.html needs — the bar and rail are on every page."""
        return {
            "q": q,
            "stage_labels": STAGE_LABELS,
            "kind_labels": KIND_LABELS,
            "coverage_labels": COVERAGE_LABELS,
            "stats": queries.stats(conn),
            "style_v": style_version(),
            "user": current_user(request),
            "unyttkassa_url": config.UNYTTKASSA_URL,
        }

    @app.get("/health")
    def health():
        """Whether this process can do its job. It used to be a constant.

        503 covers the two states where serving is pointless: our own database
        gone, or the scraper mount gone. Everything else is *reported* and not
        fatal, deliberately:

        - The archive missing is a documented degraded state — coverage simply
          does not run, and the feed says so in a banner rather than lying with
          a zero.
        - A stale tick is reported as `tick_fresh: false` for a monitor to alert
          on, but does not fail the probe. Dokku runs this as the deploy gate,
          and failing it on a dead cron would mean a broken tick blocks the
          deploy that fixes the tick.
        """
        checks = {
            "db": config.DB_PATH.exists(),
            "scraper": config.SCRAPER_DB_PATH.exists(),
            "archive": config.ARCHIVE_DB_PATH.exists(),
        }
        try:
            last = queries.stats()["last_tick"]
        except Exception:  # noqa: BLE001 — a broken DB is what we are reporting
            checks["db"] = False
            last = None
        cutoff = (datetime.now(UTC) - timedelta(hours=STALE_TICK_HOURS)).isoformat()
        checks["tick_fresh"] = bool(last and last >= cutoff)

        can_serve = checks["db"] and checks["scraper"]
        return JSONResponse(
            {
                "status": "ok" if can_serve else "degraded",
                "checks": checks,
                "last_tick": last,
            },
            status_code=200 if can_serve else 503,
        )

    @app.get("/stats", dependencies=[Depends(require_stats_token)])
    def stats_endpoint():
        # No filesystem paths: the layout of the container is not the caller's business.
        return JSONResponse({"totals": queries.stats()})

    @app.get("/api/stats")
    def api_stats(request: Request):
        """Operator status for the unyttkassa admin dashboard.

        Everything the feed used to shout at journalists about — failed calls,
        the last error, what a run cost — lives here instead, behind the same
        shared X-API-Key every other tool on the platform is polled with.
        """
        key = config.EVALS_API_KEY
        given = request.headers.get("x-api-key", "")
        if not key or not hmac.compare_digest(given.encode(), key.encode()):
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        conn = storage.connect()
        try:
            payload = {
                "totals": queries.stats(conn, fresh=True),
                "health": {
                    "db": config.DB_PATH.exists(),
                    "scraper": config.SCRAPER_DB_PATH.exists(),
                    "archive": config.ARCHIVE_DB_PATH.exists(),
                },
                "runs": queries.tick_runs(conn, limit=20),
            }
        finally:
            conn.close()
        return JSONResponse(payload)

    @app.get("/", response_class=HTMLResponse)
    def feed(
        request: Request,
        q: str | None = None,
        page: int = Query(default=1, ge=1),
        # What the search box and the filter panel send. Both are plain GET
        # forms, so this is what arrives with JavaScript off — and with it on,
        # because nothing here is enhanced away.
        sok: str | None = None,
        filtre: str | None = None,
        trinn: Annotated[list[str] | None, Query()] = None,
        type: Annotated[list[str] | None, Query()] = None,  # noqa: A002 — Norwegian form field
        kilde: Annotated[list[str] | None, Query()] = None,
        dekning: Annotated[list[str] | None, Query()] = None,
        score: str | None = None,
        dager: str | None = None,
        mur: str | None = None,
        # Pre-search links live in bookmarks and in Slack. They are translated
        # into `q` and redirected once, rather than supported forever.
        stage: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        covered: str | None = Query(default=None, pattern="^(ja|nei)$"),
        min_score: int | None = Query(default=None, ge=0, le=100),
    ):
        if q is None and any(v is not None for v in (stage, kind, source, covered, min_score)):
            legacy = searchq.from_legacy(
                stage=stage, kind=kind, source=source, covered=covered, min_score=min_score
            )
            return RedirectResponse(searchq.qs(legacy, page=page), status_code=301)

        # The controls have their own spelling; it is converted here and never
        # travels further, so there is exactly one query grammar below this line
        # and exactly one URL worth bookmarking.
        if q is None and any(
            v not in (None, [], "")
            for v in (sok, filtre, trinn, type, kilde, dekning, score, dager, mur)
        ):
            built = searchq.from_controls(
                sok=sok or "", filtre=filtre or "",
                trinn=trinn or [], type_=type or [], kilde=kilde or [],
                dekning=dekning or [],
                score=score, dager=dager, mur=mur,
                submitted=score is not None,
            )
            return RedirectResponse(searchq.qs(built), status_code=303)

        conn = storage.connect()
        try:
            # Once per render: the parser needs it to resolve `kilde:`, the
            # sidebar lists it, and the typeahead offers it.
            sources = queries.all_sources(conn)
            query = parse_q(conn, q, page, sources, hidden=mute.read(request))
            page_data = queries.feed(conn, query)
            ctx = {
                **shell_context(request, conn, query),
                "page": page_data,
                "total": page_data.total,
                "facets": queries.facet_counts(conn, query),
                "sources": sources,
                "months": MONTHS_NO,
                "total_pages": max(1, -(-page_data.total // PER_PAGE)),
                "per_page": PER_PAGE,
                "claims": queries_claims(conn, page_data),
                "stale_hours": _stale_hours(queries.stats(conn)),
                "feed_min_score": config.FEED_MIN_SCORE,
                "hidden_counts": queries.hidden_counts(conn, query),
                "back_url": _here(request),
            }
        finally:
            conn.close()
        return templates.TemplateResponse(request, "feed.html", ctx)

    @app.post("/skjul")
    def mute_word(
        request: Request,
        word: str = Form(default="", alias="ord"),
        drop: str = Form(default="", alias="fjern"),
        back: str = Form(default="/"),
    ):
        """Mute or unmute a word for this browser session.

        A cookie, not a row: the flood the desk wants out of the way is today's,
        and what one reader tires of is not what the next one is looking for.
        """
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin"}, status_code=403)
        terms = mute.read(request)
        if drop:
            terms = mute.remove(terms, drop)
        if word:
            terms = mute.add(terms, word)
        response = RedirectResponse(_safe_back(back), status_code=303)
        mute.write(response, terms, secure=request.url.scheme == "https")
        return response

    @app.get("/om", response_class=HTMLResponse)
    def about(request: Request):
        """How the ranking works and how much has been read.

        Both used to sit beside the feed, where a reader who had already read
        them once could not put them away. They are asked once; the feed is
        read every day.
        """
        conn = storage.connect()
        try:
            query = parse_q(conn, None, 1, queries.all_sources(conn))
            ctx = {
                **shell_context(request, conn, query),
                "volume": queries.daily_volume(conn, query),
                "sources": queries.source_stats(conn),
                "coverage_window_days": config.COVERAGE_WINDOW_DAYS,
            }
        finally:
            conn.close()
        return templates.TemplateResponse(request, "about.html", ctx)

    @app.get("/frafiltrert", response_class=HTMLResponse)
    def rejected(request: Request, dager: int = Query(default=7, ge=1, le=60)):
        """What the radar threw away. Without this, recall is unmeasurable."""
        conn = storage.connect()
        try:
            query = parse_q(conn, None, 1, queries.all_sources(conn))
            ctx = {
                **shell_context(request, conn, query),
                "items": queries.rejected(conn, days=dager),
                "counts": queries.rejected_counts(conn, days=dager),
                "days": dager,
                "bands": queries.score_bands(conn),
            }
        finally:
            conn.close()
        return templates.TemplateResponse(request, "rejected.html", ctx)

    @app.get("/sak", response_class=HTMLResponse)
    def story(
        request: Request,
        article_id: str = Query(alias="id"),
        q: str | None = None,
    ):
        """One story, read in full. The query rides along so back lands where you left."""
        conn = storage.connect()
        item = None
        try:
            query = parse_q(conn, q, 1, queries.all_sources(conn))
            ctx = shell_context(request, conn, query)
            item = queries.get_article(conn, article_id)
            ctx["item"] = item
            if item:
                ctx["neighbours"] = queries.same_day(conn, item)
                ctx["timeline"] = queries.story_timeline(conn, item)
                ctx["coverage"] = queries.story_coverage(item)
                ctx["claims"] = queries_claims(conn, None, article_id=item["id"])
                ctx["my_vote"] = queries_vote(conn, item["id"], current_user(request))
        finally:
            conn.close()

        if not item:
            return templates.TemplateResponse(request, "story.html", ctx, status_code=404)

        full = ScraperSource().fetch_body(item["id"])
        excerpt = item["body_excerpt"] or ""
        # The feed reads this off our truncated copy; here the untruncated body
        # is at hand, so the paywall claim is judged against what the reader
        # actually gets.
        item["body_chars"] = len(full or excerpt)
        body = full or excerpt
        # The feed's description is a teaser for most sources and the whole
        # story for some, and only the body says which. Settle it once: the
        # page prints this, and the body drops its own copy of exactly this.
        item["summary"] = standfirst(item["summary"], body)
        ctx |= {
            "body_html": article_html(
                body,
                base_url=item["link"],
                summary=item["summary"],
                source=item["source"],
                title=item["title"],
            ),
            # Only when we fell back to our own copy and it hit the storage cap.
            "truncated": full is None and len(excerpt) >= config.MAX_BODY_CHARS,
        }
        return templates.TemplateResponse(request, "story.html", ctx)

    @app.post("/sak/tilbakemelding")
    def feedback(
        request: Request,
        article_id: str = Form(alias="id"),
        vote: int = Form(),
        back: str = Form(default="/"),
    ):
        """Thumb up or down on a verdict. Finally uses the `feedback` table."""
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin"}, status_code=403)
        user = current_user(request)
        conn = storage.connect()
        try:
            storage.record_feedback(conn, article_id, user["id"], max(-1, min(1, vote)))
        finally:
            conn.close()
        return RedirectResponse(_safe_back(back), status_code=303)

    @app.post("/sak/tar")
    def claim(
        request: Request,
        article_id: str = Form(alias="id"),
        release: int = Form(default=0),
        back: str = Form(default="/"),
    ):
        """«Jeg tar denne» — so two journalists don't write the same sitatsak."""
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin"}, status_code=403)
        user = current_user(request)
        conn = storage.connect()
        try:
            if release:
                storage.release_claim(conn, article_id, user["id"])
            else:
                storage.record_claim(conn, article_id, user["id"], user["email"])
        finally:
            conn.close()
        return RedirectResponse(_safe_back(back), status_code=303)

    return app


def _stale_hours(stats: dict) -> int | None:
    """Hours since a clean tick or a story update, once worth saying.

    The desk needs one fact about the machinery: whether what they are looking
    at is today's news. Everything else about the pipeline lives on the admin
    dashboard. None means «recent enough to say nothing».
    """
    last = stats.get("last_updated")
    if not last:
        return None
    try:
        age = datetime.now(UTC) - datetime.fromisoformat(last)
    except ValueError:
        return None
    hours = int(age.total_seconds() // 3600)
    return hours if hours >= STALE_TICK_HOURS else None


def _here(request: Request) -> str:
    """This page's own path and query — where a POST should send the reader back."""
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def _safe_back(value: str) -> str:
    """Only ever redirect within this app. An open redirect is not a feature."""
    return value if value.startswith("/") and not value.startswith("//") else "/"


def queries_claims(conn, page_data, *, article_id: str | None = None) -> dict[str, dict]:
    ids = (
        [article_id]
        if article_id
        else [
            it["id"]
            for it in (page_data.items or [x for _, g in page_data.days for x in g])
        ]
    )
    return storage.claims_for(conn, ids)


def queries_vote(conn, article_id: str, user: dict) -> int | None:
    return storage.vote_for(conn, article_id, user["id"])


app = create_app()
app = SecurityHeaders(app)


def dev() -> None:
    """Local dev server. Honors PORT/HOST so `portless` can place it.

    Trusts forwarded headers for the same reason the Dockerfile does: portless
    terminates TLS, so without them every POST fails the same-origin check —
    the browser says https, the app thinks it is on http.
    """
    uvicorn.run(
        "radar.web.app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8770")),
        reload=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    dev()

"""Route handlers for the web UI."""

import asyncio
import base64
import json
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic_ai.exceptions import ContentFilterError
from starlette.responses import StreamingResponse

router = APIRouter()


def _format_error(exc: BaseException) -> str:
    """User-facing Norwegian error message; hides raw exception internals."""
    if isinstance(exc, ContentFilterError):
        return (
            "Modellens innholdsfilter blokkerte både hoved- og reservemodell. "
            "Prøv igjen, eller bytt/legg til en annen kilde."
        )
    if isinstance(exc, ValueError):
        return str(exc)
    # Unexpected exceptions can carry internals (URLs, headers, keys) — never
    # forward them to the browser; the full traceback goes to stderr.
    return "Uventet feil under generering. Prøv igjen."


def _sse(event: str | None, payload: str) -> str:
    """Format an SSE event with a JSON-encoded payload (newline-safe)."""
    data = json.dumps(payload, ensure_ascii=False)
    if event:
        return f"event: {event}\ndata: {data}\n\n"
    return f"data: {data}\n\n"


def _get_templates(request: Request):
    from .app import templates
    return templates


def _extract_title(article: str) -> str:
    """First heading (or first non-empty line) of a draft, for list display."""
    for line in article.splitlines():
        line = line.strip()
        if not line:
            continue
        return re.sub(r"^#+\s*", "", line)[:120]
    return "(uten tittel)"


@router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _get_templates(request).TemplateResponse(request, "index.html")


@router.post("/parse-urls")
async def parse_urls(request: Request) -> JSONResponse:
    """Eagerly parse URLs and cache results."""
    from kladd.profiling import Timer

    body = await request.json()
    urls = body.get("urls", [])
    if not urls:
        return JSONResponse({"results": {}})

    timer = Timer(f"parse-urls({len(urls)})")
    from kladd.sources import parse_urls_eager
    results = await asyncio.to_thread(parse_urls_eager, urls)
    timer.mark("done")
    timer.log()
    return JSONResponse({"results": results})


@router.get("/profiles", response_class=HTMLResponse)
async def profiles(request: Request) -> HTMLResponse:
    """Render profiling log as a table."""
    from kladd.storage import list_profiles

    rows = await asyncio.to_thread(list_profiles)
    tpl = _get_templates(request)
    return tpl.TemplateResponse(request, "profiles.html", {"rows": rows})


@router.get("/drafts", response_class=HTMLResponse)
async def drafts_page(request: Request) -> HTMLResponse:
    """History: the current user's drafts, newest first."""
    from kladd.storage import list_drafts

    drafts = await asyncio.to_thread(
        list_drafts, 100, user_email=request.state.user_email
    )
    for d in drafts:
        d["title"] = _extract_title(d["article"])
    tpl = _get_templates(request)
    return tpl.TemplateResponse(request, "drafts.html", {"drafts": drafts})


@router.get("/drafts/{draft_id}", response_class=HTMLResponse)
async def draft_page(request: Request, draft_id: int) -> HTMLResponse:
    """View a single previous draft."""
    from kladd.storage import get_draft

    draft = await asyncio.to_thread(
        get_draft, draft_id, user_email=request.state.user_email
    )
    tpl = _get_templates(request)
    if not draft:
        return tpl.TemplateResponse(
            request, "draft.html", {"draft": None}, status_code=404
        )
    return tpl.TemplateResponse(request, "draft.html", {"draft": draft})


def _require_admin(request: Request) -> None:
    """404 for non-admins so the page's existence isn't advertised."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=404)


@router.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request) -> HTMLResponse:
    """Admin: list and add standing rules (global or per user)."""
    from kladd.storage import known_users, list_rules

    _require_admin(request)
    rules = await asyncio.to_thread(list_rules)
    users = await asyncio.to_thread(known_users)
    tpl = _get_templates(request)
    return tpl.TemplateResponse(
        request, "rules.html", {"rules": rules, "users": users}
    )


@router.post("/rules")
async def add_rule_route(
    request: Request, text: str = Form(""), user_email: str = Form("")
) -> RedirectResponse:
    from kladd.storage import add_rule

    _require_admin(request)
    if text.strip():
        await asyncio.to_thread(add_rule, text, user_email=user_email or None)
    return RedirectResponse("/rules", status_code=303)


@router.post("/rules/{rule_id}/delete")
async def delete_rule_route(request: Request, rule_id: int) -> RedirectResponse:
    from kladd.storage import delete_rule

    _require_admin(request)
    await asyncio.to_thread(delete_rule, rule_id)
    return RedirectResponse("/rules", status_code=303)


@router.post("/draft-stream")
async def draft_stream(
    request: Request,
    urls: str = Form(""),
    instructions: str = Form(""),
    files: list[UploadFile] = File([]),
) -> StreamingResponse:
    """SSE endpoint that streams LLM draft tokens."""
    tmp_dirs: list[Path] = []
    user_email = request.state.user_email

    # Read file uploads before entering the generator (UploadFile is async)
    file_bytes: list[tuple[str, bytes]] = []
    for f in files:
        if not f.filename or not f.size:
            continue
        file_bytes.append((Path(f.filename).name, await f.read()))

    async def generate():
        from kladd.profiling import Timer
        timer = Timer("draft-stream")

        try:
            sources: list[str] = []
            for line in urls.strip().splitlines():
                line = line.strip()
                if line:
                    sources.append(line)

            for filename, data in file_bytes:
                tmp_dir = Path(tempfile.mkdtemp(prefix="kladd_"))
                tmp_dirs.append(tmp_dir)
                tmp = tmp_dir / filename
                tmp.write_bytes(data)
                sources.append(str(tmp))

            if not sources:
                yield _sse("error", "Ingen kilder oppgitt. Lim inn URLer eller last opp filer.")
                return

            from kladd.drafter import stream_draft_article
            from kladd.maalform import detect_nynorsk
            from kladd.sources import describe_error, load_and_format
            from kladd.storage import rules_for_user, save_draft, save_profile

            rules = await asyncio.to_thread(rules_for_user, user_email)
            timer.mark("setup")

            yield _sse("status", "Leser kilder …")
            loaded = await asyncio.to_thread(load_and_format, sources)
            timer.mark("parse-sources")

            for source, error in loaded.dropped:
                yield _sse("warning", f"Kilde droppet – «{source}»: {describe_error(error)}")

            sources_payload = [
                {
                    "headline": s.headline,
                    "publication": s.publication,
                    "publication_date": s.publication_date,
                    "byline": s.byline,
                    "source_type": s.source_type.value,
                    "source": s.source,
                    "markdown": s.markdown or "",
                }
                for s in loaded.parsed
            ]
            sources_b64 = base64.b64encode(
                json.dumps(sources_payload, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            yield f"event: sources\ndata: {sources_b64}\n\n"

            nynorsk = detect_nynorsk([s.markdown or "" for s in loaded.parsed])
            if nynorsk:
                yield _sse("status", "Kildene er på nynorsk — skriver utkast på nynorsk …")
            else:
                yield _sse("status", "Skriver utkast …")

            full_text = ""
            first_token = True
            stream_meta: dict = {}
            async for chunk in stream_draft_article(
                loaded.formatted,
                loaded.count,
                all_urls=loaded.all_urls,
                instructions=instructions,
                rules=rules,
                nynorsk=nynorsk,
                meta=stream_meta,
            ):
                if first_token:
                    timer.mark("first-token")
                    first_token = False
                full_text += chunk
                yield _sse(None, chunk)

            timer.mark("stream-complete")

            # Verify quotes against source material. Use the same shared
            # pipeline as /api/generate so both endpoints (and the eval
            # suite that hits /api/generate) test identical post-process
            # logic — typography, regex markers, LLM verification.
            quotes_ok = None
            quotes_json = None
            details: list[dict] = []
            from kladd.quote_check import verify_and_apply

            yield _sse("status", "Verifiserer sitater …")
            try:
                full_text, quotes_ok, quotes_json, details = await verify_and_apply(
                    full_text, loaded.parsed, nynorsk=nynorsk
                )
            except Exception as e:
                import sys, traceback
                traceback.print_exc(file=sys.stderr)
                yield _sse("warning", f"Klarte ikke verifisere sitater: {e}")

            # Re-emit the (possibly typography-cleaned and LLM-corrected)
            # final article so the rendered DOM matches what gets
            # persisted. The user has already seen the streamed tokens;
            # this is the canonical post-verification version.
            yield _sse("article", full_text)

            for d in details:
                if d["ok"]:
                    continue
                yield _sse("warning", f"Sitat ikke funnet i kildene: «{d['quote']}»")

            model_used = stream_meta.get("model", "unknown")
            draft_id = await asyncio.to_thread(
                save_draft,
                full_text,
                model_used,
                loaded.parsed,
                user_email=user_email,
                quotes_ok=quotes_ok,
                quotes_json=quotes_json,
            )
            timer.mark("save-draft")

            timer.log()
            await asyncio.to_thread(
                save_profile,
                "draft-stream",
                timer.deltas(),
                draft_id=draft_id,
                model=model_used,
                source_count=loaded.count,
                prompt_chars=len(loaded.formatted),
            )
            yield _sse("profile", timer.oneline())
            yield "event: done\ndata: \n\n"

        except Exception as e:
            import traceback, sys
            traceback.print_exc(file=sys.stderr)
            yield _sse("error", _format_error(e))

        finally:
            for d in tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)

    return StreamingResponse(generate(), media_type="text/event-stream")

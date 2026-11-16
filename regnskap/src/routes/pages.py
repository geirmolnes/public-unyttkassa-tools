"""HTML pages."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src import auth

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
# The page must come from the release that serves its JS: /static ignores ?v=, so a stored page from an older
# release gets today's app.js. Safari reuses stored pages for back/forward and tab restore without revalidating,
# even under no-cache; only no-store keeps them out of the cache (and drops one stored before).
NO_STORE = {"Cache-Control": "no-store"}
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Single-page UI; `?orgnr=` and `?modus=batch` are handled client-side."""
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "asset_version": request.app.state.asset_version,
            "email": request.state.user["email"],
            "logout_url": auth.LOGOUT_URL,
        },
        headers=NO_STORE,
    )

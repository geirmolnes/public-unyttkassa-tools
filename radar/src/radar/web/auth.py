"""Verify the unyttkassa shared session cookie.
Cookie domain: .example.org, payload {user_id, email}. Checked by session_token.py: v2
tokens (Ed25519, SESSION_PUBLIC_KEYS)."""

import hmac
import logging
import os

from fastapi import Header, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from .session_token import verify_session

logger = logging.getLogger(__name__)

COOKIE_NAME = "session"
# Where a request without a valid session is sent: the service that issues the cookie.
LOGIN_URL = os.environ.get("LOGIN_URL", "https://example.org/login")

# Who a request is from when auth is off. Not a real account: it keeps feedback
# and claims attributable to *something* locally without pretending otherwise.
LOCAL_USER = {"id": "local", "email": "local@dev"}


def auth_disabled() -> bool:
    """AUTH_DISABLED=1 turns the login gate off for local work. On Dokku it stops the app."""
    if os.environ.get("AUTH_DISABLED") != "1":
        return False
    # Dokku sets DOKKU_APP_TYPE in every container; DOKKU_APP_NAME is not set there.
    if os.environ.get("DOKKU_APP_TYPE") or os.environ.get("DOKKU_APP_NAME"):
        raise RuntimeError("AUTH_DISABLED=1 is for local work only; unset it on Dokku.")
    # Loud, because this is the line that explains why the app served a page to
    # someone who never logged in.
    logger.warning("AUTH_DISABLED=1 — every request is treated as signed in")
    return True


def get_session(request: Request) -> dict | None:
    """{user_id, email} or None."""
    return verify_session(request.cookies.get(COOKIE_NAME))


def current_user(request: Request) -> dict:
    """The signed-in user as {id, email}, or the local stand-in.

    Feedback and claims are per-person — «Kari tar denne» is the whole point —
    so the identity has to reach the template, not just the middleware.
    """
    session = getattr(request.state, "user", None) or get_session(request)
    if not session:
        return dict(LOCAL_USER)
    return {
        "id": str(session.get("user_id") or session.get("id") or "ukjent"),
        "email": session.get("email") or "",
    }


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect to unyttkassa login when cookie missing/invalid. Skips /health and /static/*."""

    async def dispatch(self, request: Request, call_next):
        # The raw ASGI path is what the router dispatches on; request.url.path is rebuilt
        # from the Host header, so a poisoned Host could fake an exempt path (CVE-2026-48710).
        path = request.scope["path"]
        # /stats (X-Stats-Token, require_stats_token) and /api/stats (X-API-Key) check
        # their own header: they are polled server-side, with no session cookie of ours.
        if path in ("/health", "/stats", "/api/stats") or path.startswith("/static/"):
            return await call_next(request)

        session = get_session(request)
        if not session:
            return RedirectResponse(LOGIN_URL, status_code=302)

        request.state.user = session
        return await call_next(request)


def require_stats_token(x_stats_token: str = Header("")) -> None:
    """401 unless X-Stats-Token matches STATS_TOKEN. Unset STATS_TOKEN admits nobody."""
    expected = os.environ.get("STATS_TOKEN", "")
    if not expected or not hmac.compare_digest(x_stats_token.encode(), expected.encode()):
        raise HTTPException(401, "Invalid or missing stats token")

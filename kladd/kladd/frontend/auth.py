"""Auth middleware — verify the unyttkassa session cookie (Ed25519 v2 tokens, session_token.py)."""

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from .session_token import verify_session

COOKIE_NAME = "session"
# Where a request without a valid session is sent: the service that issues the cookie.
LOGIN_URL = os.environ.get("LOGIN_URL", "https://example.org/login")


# Who may manage standing rules at /rules (comma-separated).
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("KLADD_ADMIN_EMAILS", "admin@example.org").split(",")
    if e.strip()
}

# sitatkladd also serves the old kladd.example.org host; 301 it to the new name.
LEGACY_HOST = "kladd.example.org"
LEGACY_REDIRECT_BASE = "https://sitatkladd.example.org"


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect to unyttkassa login if session cookie is invalid."""

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host == LEGACY_HOST:
            target = LEGACY_REDIRECT_BASE + request.url.path
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(target, status_code=301)

        # The raw ASGI path is what the router dispatches on; request.url.path is rebuilt
        # from the Host header, so a poisoned Host could fake an exempt path (CVE-2026-48710).
        path = request.scope["path"]
        if path == "/health" or path.startswith("/api/"):
            return await call_next(request)

        # verify_session returns nothing for a signed token that names no user.
        session = verify_session(request.cookies.get(COOKIE_NAME))
        if session is None:
            return RedirectResponse(LOGIN_URL, status_code=302)
        email = session["email"]
        request.state.user_email = email
        request.state.is_admin = email.lower() in ADMIN_EMAILS

        return await call_next(request)

"""Shared unyttkassa session cookie auth.

Verifies the `session` cookie set by example.org's Google OAuth flow, with
src/session_token.py: v2 tokens (Ed25519, SESSION_PUBLIC_KEYS).

Users who aren't logged in are redirected to the central login page.
"""

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from .session_token import verify_session

COOKIE_NAME = "session"
# Where a request without a valid session is sent: the service that issues the cookie.
LOGIN_URL = os.environ.get("LOGIN_URL", "https://example.org/login")

# Paths that never require session auth (health checks, etc).
PUBLIC_PATHS = {"/healthz"}
# Prefixes routed to endpoints that authenticate themselves (API key header)
# rather than via the shared session cookie. The endpoint guards its own
# access — skipping the middleware just means the cookie redirect doesn't
# clobber the 401.
PUBLIC_PREFIXES = ("/api/stats",)


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect to unyttkassa login if the session cookie is missing or invalid.

    On success, puts the authenticated user's email on request.state.user_email
    so downstream handlers can scope data to them.
    """

    async def dispatch(self, request: Request, call_next):
        # The raw ASGI path is what the router dispatches on; request.url.path is rebuilt
        # from the Host header, so a poisoned Host could fake an exempt path (CVE-2026-48710).
        path = request.scope["path"]
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        payload = verify_session(request.cookies.get(COOKIE_NAME))
        # Only a session carries an email.
        email = payload.get("email") if isinstance(payload, dict) else None
        if not email:
            return RedirectResponse(LOGIN_URL, status_code=302)

        request.state.user_email = email
        return await call_next(request)


def get_user_email(request: Request) -> str:
    """FastAPI dependency: return the authenticated user's email.

    The middleware runs first and guarantees this is set on protected paths.
    """
    return request.state.user_email

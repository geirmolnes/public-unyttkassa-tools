"""Shared unyttkassa session cookie auth.

Verifies the `session` cookie set by example.org (payload {user_id, email}) with
src/session_token.py: v2 tokens (Ed25519, SESSION_PUBLIC_KEYS). Missing/invalid cookie →
redirect to the central login page.
"""

import os
from collections.abc import Mapping

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.session_token import verify_session

DEV_EMAIL = "dev@localhost"


def dev_mode(env: Mapping[str, str] = os.environ) -> bool:
    """Whether DEV_INSECURE_AUTH=1 turns the login off. Raises if the flag is set in prod."""
    if env.get("DEV_INSECURE_AUTH") != "1":
        return False
    # Dokku sets DOKKU_APP_TYPE in every container; DOKKU_APP_NAME is not set there.
    if env.get("DOKKU_APP_TYPE") or env.get("DOKKU_APP_NAME"):
        raise RuntimeError(
            "DEV_INSECURE_AUTH=1 is set in production; it turns the login off. Unset it."
        )
    return True


DEV_MODE = dev_mode()
if DEV_MODE:
    print(
        "[regnskap.auth] WARNING: DEV_INSECURE_AUTH=1 — auth bypassed. Never use in prod.",
        flush=True,
    )

COOKIE_NAME = "session"
# The login service owns the session cookie, so logging out is its job too.
LOGIN_URL = os.environ.get("LOGIN_URL", "https://example.org/login")
LOGOUT_URL = os.environ.get("LOGOUT_URL", "https://example.org/logout")
PUBLIC_PATHS = {"/health"}
# Who may read /api/diagnostics: it shows who read which company, which colleagues must not see
ADMINS = {
    e.strip().lower()
    for e in os.environ.get("REGNSKAP_ADMINS", "admin@example.org").split(",")
    if e.strip()
} | ({DEV_EMAIL} if DEV_MODE else set())


def get_session(request: Request) -> dict | None:
    """Verified cookie payload {user_id, email}, or None."""
    payload = verify_session(request.cookies.get(COOKIE_NAME))
    return payload if isinstance(payload, dict) and payload.get("email") else None


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect to unyttkassa login if the session cookie is missing/invalid. Skips /health."""

    async def dispatch(self, request: Request, call_next):
        # The raw ASGI path is what the router dispatches on; request.url.path is rebuilt
        # from the Host header, so a poisoned Host could fake an exempt path (CVE-2026-48710).
        if request.scope["path"] in PUBLIC_PATHS:
            return await call_next(request)

        session = get_session(request)
        if not session and DEV_MODE:
            session = {"user_id": 0, "email": DEV_EMAIL}
        if not session:
            return RedirectResponse(LOGIN_URL, status_code=302)

        request.state.user = session
        return await call_next(request)


def get_user_email(request: Request) -> str:
    """FastAPI dependency: authenticated user's email (set by the middleware)."""
    return request.state.user["email"]


def require_admin(request: Request) -> str:
    """FastAPI dependency: the user's email, or 403 unless they are in ADMINS."""
    email = get_user_email(request)
    if email.lower() not in ADMINS:
        raise HTTPException(403, f"Bare for administrator. Du er logget inn som {email}.")
    return email

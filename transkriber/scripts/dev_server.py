"""Run transkriber locally without a login service.

Starts the real app with a throwaway signing key and puts /dev-login in front of it, which
sets the same signed `session` cookie the app verifies. A real login service does what
/dev-login does, after it has checked who the user is.

    uv run python scripts/dev_server.py
"""

import os
import sys
from pathlib import Path

import uvicorn
from starlette.responses import RedirectResponse
from starlette.types import Receive, Scope, Send

# `src` is not an installed package: it is found from the repo root, which is not on the
# path when this file is run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.session_token import generate_keypair, issue_session  # noqa: E402

if os.environ.get("DOKKU_APP_TYPE") or os.environ.get("DOKKU_APP_NAME"):
    sys.exit("scripts/dev_server.py is for local work only")

DEV_EMAIL = os.environ.get("DEV_EMAIL", "dev@example.org")

# The app reads its settings at import, so they are set before it is imported.
_signing_key, _public_key = generate_keypair("dev")
os.environ.update(
    SESSION_SIGNING_KEY=_signing_key,
    SESSION_PUBLIC_KEYS=_public_key,
    SESSION_ISSUER="localhost",
    LOGIN_URL="/dev-login",
    LOGOUT_URL="/dev-login",
)

from src.main import app  # noqa: E402


async def dev_app(scope: Scope, receive: Receive, send: Send) -> None:
    """The real app, with /dev-login in front of its login gate."""
    if scope["type"] != "http" or scope["path"] != "/dev-login":
        await app(scope, receive, send)
        return
    response = RedirectResponse("/", status_code=303)
    cookie = issue_session({"user_id": 0, "email": DEV_EMAIL})
    response.set_cookie("session", cookie, httponly=True, samesite="lax")
    await response(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(dev_app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))

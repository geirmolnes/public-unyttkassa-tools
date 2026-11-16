"""Shared pytest fixtures.

The tests hit the real FastAPI app via httpx TestClient and use a temporary
upload dir + SQLite DB per test so they're isolated. A throwaway Ed25519 key lets
us mint valid session cookies without going through unyttkassa's OAuth flow.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from src.session_token import (
    MAX_AGE,
    generate_keypair,
    parse_signing_key,
    session_settings,
    sign_v2,
)

TEST_EMAIL = "journalist@example.com"
TEST_ISSUER = "example.org"
TEST_SIGNING_KEY, TEST_PUBLIC_KEY = generate_keypair("ukd-test")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Point the app at a throwaway upload dir + DB for every test."""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.delenv("SECRET_KEY", raising=False)
    # Login signing: a throwaway Ed25519 key.
    monkeypatch.setenv("SESSION_PUBLIC_KEYS", TEST_PUBLIC_KEY)
    monkeypatch.setenv("SESSION_ISSUER", TEST_ISSUER)
    session_settings.cache_clear()
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("TRANSKRIBER_DB", str(upload_dir / "transkriber.db"))
    # Stop ruff: ensure nothing reads a real API key during unit tests.
    monkeypatch.setenv("GEMINI_API_KEY", "unused-in-unit-tests")

    # Force module reload so the config picks up the new env vars.
    import importlib

    import src.auth as auth_module
    import src.db as db_module
    import src.main as main_module

    importlib.reload(auth_module)
    importlib.reload(db_module)
    importlib.reload(main_module)
    yield
    session_settings.cache_clear()


def _signed_cookie(email: str, user_id: int = 1) -> str:
    """Produce a v2 session cookie like the one example.org issues."""
    claims = {"user_id": user_id, "email": email}
    return sign_v2(
        claims, parse_signing_key(TEST_SIGNING_KEY), issuer=TEST_ISSUER, now=int(time.time())
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def signed_body(body: object) -> str:
    """A v2 token over any JSON body, signed with the test key past sign_v2's own claim checks."""
    if isinstance(body, dict):
        now = int(time.time())
        body = {"iss": TEST_ISSUER, "kid": "ukd-test", **body, "iat": now, "exp": now + 3600}
    payload = _b64(json.dumps(body).encode())
    sig = parse_signing_key(TEST_SIGNING_KEY).key.sign(f"v2.{payload}".encode("ascii"))
    return f"v2.{payload}.{_b64(sig)}"


@pytest.fixture
def client():
    """TestClient that sends the authenticated session cookie on every request.

    We set it as a default header rather than via the cookie jar because httpx's
    jar enforces domain matching against the base URL, which causes the jar to
    drop the cookie when the test server redirects externally.
    """
    from fastapi.testclient import TestClient

    import src.main as main_module

    c = TestClient(
        main_module.app,
        headers={"Cookie": f"session={_signed_cookie(TEST_EMAIL)}"},
    )
    return c


@pytest.fixture
def mint_v2():
    """Mint a v2 session token; defaults to the key and issuer the app trusts."""

    def mint(
        claims: dict,
        *,
        signing_key: str = TEST_SIGNING_KEY,
        issuer: str = TEST_ISSUER,
        age: int = 0,
        ttl: int = MAX_AGE,
    ) -> str:
        now = int(time.time()) - age
        return sign_v2(claims, parse_signing_key(signing_key), issuer=issuer, now=now, ttl=ttl)

    return mint

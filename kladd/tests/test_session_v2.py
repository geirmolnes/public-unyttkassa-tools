"""Login cookie: only v2 (Ed25519) tokens open kladd; admin rights stay with KLADD_ADMIN_EMAILS."""

import base64
import hashlib
import hmac
import json
import os
import time

os.environ.setdefault("EVALS_API_KEY", "test-evals-key")

import pytest
from fastapi.testclient import TestClient

from kladd.frontend.app import app
from kladd.frontend.auth import LOGIN_URL
from kladd.frontend.session_token import session_settings

from .conftest import v2_token

REPORTER = {"user_id": 7, "email": "Journalist@Example.no"}


def legacy_cookie(secret: str, claims: dict, now: int | None = None) -> str:
    """A cookie in the retired itsdangerous format (URLSafeTimedSerializer, default salt)."""

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = b64(json.dumps(claims, separators=(",", ":")).encode())
    at = int(time.time()) if now is None else now
    value = f"{payload}.{b64(at.to_bytes(4, 'big'))}"
    key = hashlib.sha1(b"itsdangerous" + b"signer" + secret.encode()).digest()
    return f"{value}.{b64(hmac.new(key, value.encode(), hashlib.sha1).digest())}"


# The pre-v2 cookie, correctly signed for SECRET_KEY="legacy-test-secret". Minted at test time: a
# fixed literal would age past the old 30-day max_age and then prove nothing.
OLD_COOKIE = legacy_cookie(
    "legacy-test-secret", {"user_id": 7, "email": "kollega@example.no"}
)


def v2(claims: dict = REPORTER, **kw) -> str:
    return v2_token(claims, **kw)


def get(path: str, token: str) -> tuple[int, str | None]:
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("session", token)
    r = client.get(path)
    return r.status_code, r.headers.get("location")


@pytest.mark.parametrize(
    ("make_token", "expected"),
    [
        (lambda: v2(), (200, None)),
        (lambda: v2(key="B"), (302, LOGIN_URL)),
        (lambda: v2(age=61, ttl=60), (302, LOGIN_URL)),
        (lambda: v2(age=59, ttl=60), (200, None)),
        (lambda: v2(issuer="example.net"), (302, LOGIN_URL)),
    ],
    ids=[
        "v2",
        "v2-unpublished-key",
        "v2-expired-1s-ago",
        "v2-expires-in-1s",
        "v2-other-fleet",
    ],
)
def test_session_cookie(tmp_db, make_token, expected):
    assert get("/", make_token()) == expected


def test_old_format_cookie_is_refused_even_with_the_old_flag_and_key(
    tmp_db, monkeypatch
):
    monkeypatch.setenv("SESSION_ACCEPT_LEGACY", "1")
    monkeypatch.setenv("SECRET_KEY", "legacy-test-secret")
    session_settings.cache_clear()
    try:
        assert get("/", OLD_COOKIE) == (302, LOGIN_URL)
    finally:
        session_settings.cache_clear()


@pytest.mark.parametrize(
    ("make_token", "status"),
    [(lambda: v2({"email": "Redaktor@Example.no"}), 200), (lambda: v2(), 404)],
    ids=["v2-admin", "v2-non-admin"],
)
def test_admin_emails_still_gate_rules(tmp_db, monkeypatch, make_token, status):
    monkeypatch.setattr("kladd.frontend.auth.ADMIN_EMAILS", {"redaktor@example.no"})
    assert get("/rules", make_token())[0] == status


def test_legacy_cookie_matches_a_real_itsdangerous_token():
    """At a fixed time the helper reproduces, byte for byte, a token itsdangerous 2.2 minted."""
    claims = {"user_id": 7, "email": "kollega@example.no"}
    assert legacy_cookie("legacy-test-secret", claims, now=1_794_219_054) == (
        "eyJ1c2VyX2lkIjo3LCJlbWFpbCI6ImtvbGxlZ2FAZXhhbXBsZS5ubyJ9.avGcLg.gVn_UWNcyzacHDFq1COCQZWggZA"
    )

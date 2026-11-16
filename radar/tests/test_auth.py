"""The login gate: only v2 (Ed25519) session tokens get in.

The suite runs with AUTH_DISABLED=1 (conftest), so these tests drive AuthMiddleware on a minimal app.
"""

import base64
import hashlib
import hmac
import importlib
import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from radar.web import auth
from radar.web.session_token import generate_keypair, parse_signing_key, session_settings, sign_v2

ISSUER = "example.org"
SIGNING_KEY, PUBLIC_KEY = generate_keypair("ukd-test")
OTHER_SIGNING_KEY, _ = generate_keypair("ukd-other")
CLAIMS = {"user_id": 7, "email": "kari@example.no"}
LOGIN = (302, "https://example.org/login")


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
OLD_COOKIE = legacy_cookie("legacy-test-secret", {"user_id": 7, "email": "kollega@example.no"})


@pytest.fixture(autouse=True)
def session_env(monkeypatch):
    """The key and issuer the app trusts."""
    monkeypatch.setenv("SESSION_PUBLIC_KEYS", PUBLIC_KEY)
    monkeypatch.setenv("SESSION_ISSUER", ISSUER)
    session_settings.cache_clear()
    yield
    session_settings.cache_clear()


@pytest.fixture
def gated() -> TestClient:
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)

    @app.get("/")
    def index(request: Request):
        return auth.current_user(request)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app, follow_redirects=False)


def v2(
    claims: dict = CLAIMS,
    *,
    key: str = SIGNING_KEY,
    issuer: str = ISSUER,
    age: int = 0,
    ttl: int = 3600,
) -> str:
    """A v2 session token issued `age` seconds ago."""
    return sign_v2(
        claims, parse_signing_key(key), issuer=issuer, now=int(time.time()) - age, ttl=ttl
    )


def status(
    client: TestClient, token: str | None, headers: dict | None = None
) -> tuple[int, str | None]:
    client.cookies.clear()
    if token is not None:
        client.cookies.set("session", token)
    response = client.get("/", headers=headers or {})
    return response.status_code, response.headers.get("location")


@pytest.mark.parametrize(
    "make,expected",
    [
        (lambda: v2(), (200, None)),
        (lambda: v2(key=OTHER_SIGNING_KEY), LOGIN),
        (lambda: v2(issuer="example.net"), LOGIN),
        (lambda: v2(age=61, ttl=60), LOGIN),
        (lambda: v2(age=55, ttl=60), (200, None)),
        (lambda: OLD_COOKIE, LOGIN),
        (lambda: "v2.tull.tull", LOGIN),
        (lambda: "", LOGIN),
        (lambda: None, LOGIN),
    ],
    ids=[
        "v2-valid",
        "v2-other-key",
        "v2-other-fleet",
        "v2-expired-1s-ago",
        "v2-expires-in-5s",
        "old-format",
        "v2-garbage",
        "empty-cookie",
        "no-cookie",
    ],
)
def test_gate(gated, make, expected):
    assert status(gated, make()) == expected


def test_old_format_cookie_is_refused_even_with_the_old_flag_and_key(gated, monkeypatch):
    monkeypatch.setenv("SESSION_ACCEPT_LEGACY", "1")
    monkeypatch.setenv("SECRET_KEY", "legacy-test-secret")
    session_settings.cache_clear()
    assert status(gated, OLD_COOKIE) == LOGIN
    assert status(gated, v2()) == (200, None)


def test_v2_user_reaches_the_page(gated):
    """Feedback and claims are per person, so the v2 identity must reach current_user()."""
    gated.cookies.set("session", v2({"user_id": 7, "email": "Kari@Example.no"}))
    assert gated.get("/").json() == {"id": "7", "email": "kari@example.no"}


@pytest.mark.parametrize("terminator", ["?", "#"], ids=["query", "fragment"])
@pytest.mark.parametrize(
    "exempt", ["/health", "/stats", "/static/x"], ids=["health", "stats", "static"]
)
def test_a_poisoned_host_header_does_not_open_the_page(gated, exempt, terminator):
    """CVE-2026-48710 class: exemptions read scope["path"], never the Host-derived request.url.path."""
    assert status(gated, None, {"host": f"testserver{exempt}{terminator}"}) == LOGIN


def test_legacy_cookie_matches_a_real_itsdangerous_token():
    """At a fixed time the helper reproduces, byte for byte, a token itsdangerous 2.2 minted."""
    claims = {"user_id": 7, "email": "kollega@example.no"}
    assert legacy_cookie("legacy-test-secret", claims, now=1_794_219_054) == (
        "eyJ1c2VyX2lkIjo3LCJlbWFpbCI6ImtvbGxlZ2FAZXhhbXBsZS5ubyJ9.avGcLg.gVn_UWNcyzacHDFq1COCQZWggZA"
    )


@pytest.mark.parametrize(
    "env,expected",
    [({}, "https://example.org/login"), ({"LOGIN_URL": "/inn"}, "/inn")],
    ids=["default", "overridden"],
)
def test_the_login_url_comes_from_the_environment(monkeypatch, env, expected):
    monkeypatch.delenv("LOGIN_URL", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    try:
        importlib.reload(auth)
        app = FastAPI()
        app.add_middleware(auth.AuthMiddleware)
        response = TestClient(app, follow_redirects=False).get("/")
        assert (response.status_code, response.headers["location"]) == (302, expected)
    finally:
        monkeypatch.undo()
        importlib.reload(auth)

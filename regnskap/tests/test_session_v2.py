"""Session cookie check: only v2 (Ed25519) tokens count; the dev login bypass is local-only."""

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import auth
from src.session_token import generate_keypair, parse_signing_key, sign_v2
from tests.conftest import TEST_EMAIL, TEST_ISSUER, TEST_PUBLIC_KEY, TEST_SIGNING_KEY

REPO = Path(__file__).parent.parent
OTHER_SIGNING_KEY, _ = generate_keypair("ukd-other")
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


def v2(
    email: str = TEST_EMAIL,
    *,
    key: str = TEST_SIGNING_KEY,
    issuer: str = TEST_ISSUER,
    age: int = 0,
    ttl: int = 3600,
) -> str:
    """A v2 session token issued `age` seconds ago."""
    claims = {"user_id": 1, "email": email}
    return sign_v2(
        claims, parse_signing_key(key), issuer=issuer, now=int(time.time()) - age, ttl=ttl
    )


def status(cookie: str | None, path: str = "/api/recent") -> tuple[int, str | None]:
    from src.main import app

    headers = {"Cookie": f"session={cookie}"} if cookie is not None else {}
    with TestClient(app, headers=headers, follow_redirects=False) as c:
        resp = c.get(path)
    return resp.status_code, resp.headers.get("location")


@pytest.mark.parametrize(
    "make,expected",
    [
        (lambda: v2(), (200, None)),
        (lambda: v2(key=OTHER_SIGNING_KEY), LOGIN),
        (lambda: v2(issuer="example.net"), LOGIN),
        (lambda: v2(age=61, ttl=60), LOGIN),
        (lambda: v2(age=55, ttl=60), (200, None)),
        (lambda: "v2.tull.tull", LOGIN),
    ],
    ids=[
        "v2-valid",
        "v2-other-key",
        "v2-other-fleet",
        "v2-expired-1s-ago",
        "v2-expires-in-5s",
        "v2-garbage",
    ],
)
def test_session(brreg, make, expected):
    assert status(make()) == expected


@pytest.mark.parametrize("accept_legacy", ["1", "0"])
def test_an_old_format_cookie_is_refused(brreg, monkeypatch, accept_legacy):
    """Only v2 counts, whatever SESSION_ACCEPT_LEGACY and SECRET_KEY are left in the config."""
    monkeypatch.setenv("SESSION_ACCEPT_LEGACY", accept_legacy)
    monkeypatch.setenv("SECRET_KEY", "legacy-test-secret")
    assert status(OLD_COOKIE) == LOGIN


def test_a_login_works_with_SECRET_KEY_set(brreg, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "legacy-test-secret")
    assert status(v2()) == (200, None)


@pytest.mark.parametrize(
    "email,expected",
    [("admin@example.com", 200), ("ADMIN@example.com", 200), (TEST_EMAIL, 403)],
    ids=["admin", "admin-uppercase", "journalist"],
)
def test_diagnostics_stays_admin_only_with_v2(brreg, email, expected):
    assert status(v2(email), "/api/diagnostics")[0] == expected


@pytest.mark.parametrize("terminator", ["?", "#"], ids=["query", "fragment"])
def test_a_poisoned_host_header_does_not_open_the_api(brreg, terminator):
    """CVE-2026-48710 class: the /health exemption reads scope["path"], not request.url.path."""
    from src.main import app

    with TestClient(app, follow_redirects=False) as c:
        resp = c.get("/api/recent", headers={"host": f"testserver/health{terminator}"})
    assert (resp.status_code, resp.headers.get("location")) == LOGIN


# ---------------------------------------------------------------------------------------
# The dev bypass (DEV_INSECURE_AUTH=1): local only, never tied to SECRET_KEY


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, False),
        ({"DEV_INSECURE_AUTH": "0"}, False),
        ({"DEV_INSECURE_AUTH": "1"}, True),
        ({"DEV_INSECURE_AUTH": "1", "SECRET_KEY": "legacy-test-secret"}, True),
        ({"DOKKU_APP_TYPE": "dockerfile"}, False),
        ({"DOKKU_APP_TYPE": "dockerfile", "DEV_INSECURE_AUTH": "0"}, False),
    ],
    ids=["unset", "off", "on", "on-with-secret", "prod-unset", "prod-off"],
)
def test_dev_mode(env, expected):
    assert auth.dev_mode(env) is expected


@pytest.mark.parametrize("prod_var", ["DOKKU_APP_TYPE", "DOKKU_APP_NAME"])
@pytest.mark.parametrize("secret", [None, "legacy-test-secret"], ids=["no-secret", "secret"])
def test_dev_mode_in_prod_refuses_to_start(prod_var, secret):
    env = {prod_var: "dockerfile", "DEV_INSECURE_AUTH": "1"}
    if secret:
        env["SECRET_KEY"] = secret
    with pytest.raises(RuntimeError, match="DEV_INSECURE_AUTH"):
        auth.dev_mode(env)


@pytest.mark.parametrize("secret", [None, "legacy-test-secret"], ids=["no-secret", "secret"])
def test_the_dev_bypass_lets_an_anonymous_request_in_locally(brreg, monkeypatch, secret):
    """The bypass follows the flag alone; a SECRET_KEY in the dev shell doesn't switch it off."""
    monkeypatch.setattr(auth, "DEV_MODE", True)
    if secret:
        monkeypatch.setenv("SECRET_KEY", secret)
    else:
        monkeypatch.delenv("SECRET_KEY", raising=False)
    assert status(None) == (200, None)


def test_a_real_session_wins_over_the_dev_bypass(brreg, monkeypatch):
    """With the bypass on, a real cookie is still read: the admin check sees its email."""
    monkeypatch.setattr(auth, "DEV_MODE", True)
    monkeypatch.setattr(auth, "ADMINS", {"admin@example.com"})
    assert status(None, "/api/diagnostics")[0] == 403  # dev@localhost
    assert status(v2("admin@example.com"), "/api/diagnostics")[0] == 200


# ---------------------------------------------------------------------------------------
# Prod start-up (fresh interpreter, Dokku-like env)

ANON_PROBE = (
    "from fastapi.testclient import TestClient\n"
    "r = TestClient(src.main.app).get('/api/recent', follow_redirects=False)\n"
    "assert (r.status_code, r.headers.get('location')) == "
    "(302, 'https://example.org/login'), r.status_code\n"
)


def _import_in_prod(extra: dict[str, str], probe: str = "") -> subprocess.CompletedProcess:
    """Import src.main with DOKKU_APP_TYPE and the session config, no SECRET_KEY or dev flag."""
    drop = ("SECRET_KEY", "DEV_INSECURE_AUTH", "SESSION_ACCEPT_LEGACY", "DOKKU_APP_NAME")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env |= {
        "DOKKU_APP_TYPE": "dockerfile",
        "SESSION_PUBLIC_KEYS": TEST_PUBLIC_KEY,
        "SESSION_ISSUER": TEST_ISSUER,
        **extra,
    }
    return subprocess.run(
        [sys.executable, "-c", f"import src.main\n{probe}"],
        check=False,
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "extra", [{}, {"SECRET_KEY": "legacy-test-secret"}], ids=["no-secret", "secret-set"]
)
def test_the_app_starts_in_prod_with_or_without_SECRET_KEY_and_gates_anonymous(extra):
    result = _import_in_prod(extra, ANON_PROBE)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "extra", [{}, {"SECRET_KEY": "legacy-test-secret"}], ids=["no-secret", "secret-set"]
)
def test_the_app_refuses_to_start_in_prod_with_the_dev_flag(extra):
    result = _import_in_prod({"DEV_INSECURE_AUTH": "1", **extra})
    assert result.returncode != 0
    assert "DEV_INSECURE_AUTH" in result.stderr


def test_legacy_cookie_matches_a_real_itsdangerous_token():
    """At a fixed time the helper reproduces, byte for byte, a token itsdangerous 2.2 minted."""
    claims = {"user_id": 7, "email": "kollega@example.no"}
    assert legacy_cookie("legacy-test-secret", claims, now=1_794_219_054) == (
        "eyJ1c2VyX2lkIjo3LCJlbWFpbCI6ImtvbGxlZ2FAZXhhbXBsZS5ubyJ9.avGcLg.gVn_UWNcyzacHDFq1COCQZWggZA"
    )

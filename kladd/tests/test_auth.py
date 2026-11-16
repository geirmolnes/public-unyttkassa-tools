"""Auth middleware: the public-path exemption can't be reached through the Host header,
and only a signed session that names a user gets in."""

import os
import time

os.environ.setdefault("EVALS_API_KEY", "test-evals-key")

import pytest
from fastapi.testclient import TestClient

from kladd.frontend.app import app

from .conftest import v2_signed


@pytest.mark.parametrize("terminator", ["?", "#"], ids=["query", "fragment"])
@pytest.mark.parametrize("exempt", ["/health", "/api/x"], ids=["health", "api-prefix"])
def test_a_poisoned_host_header_does_not_open_the_app(exempt, terminator):
    # CVE-2026-48710: starlette <1.0.1 built request.url from the raw Host header, so
    # "testserver/health?" made request.url.path read "/health" while "/" was routed.
    response = TestClient(app).get(
        "/", headers={"Host": f"testserver{exempt}{terminator}"}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "https://example.org/login"


def signed(payload: object) -> str:
    """A current, validly signed v2 token; dict payloads get iss/kid/iat/exp, others are signed as is."""
    if not isinstance(payload, dict):
        return v2_signed(payload)
    now = int(time.time())
    return v2_signed(
        {
            "iss": "example.org",
            "kid": "test-A",
            **payload,
            "iat": now,
            "exp": now + 3600,
        }
    )


# A valid signature alone does not make a payload a session: it must name a user.
SESSION_PAYLOADS = pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"user_id": 1, "email": "a@x.no"}, 200),
        ({"user_id": 1, "email": "a"}, 200),
        ({"user_id": 1, "email": ""}, 302),
        ({"user_id": 1}, 302),
        ({"purpose": "login", "uid": 1}, 302),
        ("a@x.no", 302),
        (None, 302),
    ],
    ids=[
        "hub-session",
        "one-char-email",
        "empty-email",
        "no-email",
        "magic-link-shape",
        "bare-string",
        "null",
    ],
)


@SESSION_PAYLOADS
def test_only_a_signed_session_with_an_email_gets_in(tmp_db, payload, status):
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    client.cookies.set("session", signed(payload))

    response = client.get("/")

    assert response.status_code == status


def test_old_kladd_host_redirects_to_sitatkladd():
    response = TestClient(app).get(
        "/drafts?x=1", headers={"Host": "kladd.example.org"}, follow_redirects=False
    )

    assert response.status_code == 301
    assert (
        response.headers["location"] == "https://sitatkladd.example.org/drafts?x=1"
    )

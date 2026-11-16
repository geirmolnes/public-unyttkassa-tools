"""Prod (Dokku) startup: no SECRET_KEY needed, login always on, AUTH_DISABLED refused."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_auth import CLAIMS, ISSUER, OLD_COOKIE, PUBLIC_KEY, v2

from radar.web import auth

REPO = Path(__file__).resolve().parent.parent

# Imports the app in a fresh interpreter (env read at import, as in the container) and
# prints the status of GET / for each named cookie.
PROBE = """
import json, sys
from starlette.testclient import TestClient
from radar.web.app import app

client = TestClient(app, follow_redirects=False)
out = {}
for name, cookie in json.loads(sys.argv[1]).items():
    client.cookies.clear()
    if cookie:
        client.cookies.set("session", cookie)
    out[name] = client.get("/").status_code
print(json.dumps(out))
"""

_SCRUBBED = ("AUTH_DISABLED", "SECRET_KEY", "SESSION_ACCEPT_LEGACY", "DOKKU_APP_NAME")


def prod_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Dokku-like env: DOKKU_APP_TYPE set, the test public key, data under tmp_path."""
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    return {
        **env,
        "DOKKU_APP_TYPE": "dockerfile",
        "SESSION_PUBLIC_KEYS": PUBLIC_KEY,
        "SESSION_ISSUER": ISSUER,
        "RADAR_DATA_DIR": str(tmp_path),
        "SCRAPER_DB_PATH": str(tmp_path / "news_scraper.db"),
        "ARCHIVE_DB_PATH": str(tmp_path / "unytt_artikler.db"),
        **extra,
    }


def run(code: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        check=False,
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    "extra",
    [{}, {"SECRET_KEY": "legacy-test-secret", "SESSION_ACCEPT_LEGACY": "1"}],
    ids=["no-secret-key", "secret-key-and-old-flag"],
)
def test_prod_starts_and_only_v2_logs_in(tmp_path, extra):
    cookies = {"anonymous": None, "old_cookie": OLD_COOKIE, "v2": v2(CLAIMS)}

    result = run(PROBE, prod_env(tmp_path, **extra), json.dumps(cookies))

    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "anonymous": 302,
        "old_cookie": 302,
        "v2": 200,
    }


@pytest.mark.parametrize("app_var", ["DOKKU_APP_TYPE", "DOKKU_APP_NAME"])
def test_prod_refuses_to_start_with_auth_disabled(tmp_path, app_var):
    env = prod_env(tmp_path, AUTH_DISABLED="1")
    env.pop("DOKKU_APP_TYPE")
    env[app_var] = "radar"

    result = run("import radar.web.app", env)

    assert result.returncode != 0
    assert "AUTH_DISABLED=1" in result.stderr


@pytest.mark.parametrize(
    ("env", "expected"),
    [({}, False), ({"AUTH_DISABLED": "1"}, True), ({"AUTH_DISABLED": "0"}, False)],
    ids=["unset", "local-opt-out", "zero"],
)
def test_auth_disabled_locally(monkeypatch, env, expected):
    for name in ("AUTH_DISABLED", "DOKKU_APP_TYPE", "DOKKU_APP_NAME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert auth.auth_disabled() is expected

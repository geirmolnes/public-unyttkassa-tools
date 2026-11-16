"""Prod (Dokku) startup: no SECRET_KEY needed, login always on, dev secrets never accepted."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kladd.env import require_env

from .conftest import v2_token
from .test_session_v2 import OLD_COOKIE

REPO = Path(__file__).resolve().parent.parent
DEV_API_KEY = "dev-insecure-api-key"

# Imports the app in a fresh interpreter (env read at import, as in the container) and
# prints the status of GET / for each named cookie.
PROBE = """
import json, sys
from fastapi.testclient import TestClient
from kladd.frontend.app import app

client = TestClient(app, follow_redirects=False)
out = {}
for name, cookie in json.loads(sys.argv[1]).items():
    client.cookies.clear()
    if cookie:
        client.cookies.set("session", cookie)
    out[name] = client.get("/").status_code
print(json.dumps(out))
"""

_SCRUBBED = (
    "SECRET_KEY",
    "SESSION_ACCEPT_LEGACY",
    "EVALS_API_KEY",
    "KLADD_DEV",
    "DOKKU_APP_NAME",
)


def prod_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Dokku-like env: DOKKU_APP_TYPE set, session config and dummy model keys from conftest."""
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    return {
        **env,
        "DOKKU_APP_TYPE": "dockerfile",
        "KLADD_DATA_DIR": str(tmp_path),
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
    [
        {},
        {"SECRET_KEY": "legacy-test-secret", "SESSION_ACCEPT_LEGACY": "1"},
        {"KLADD_DEV": "1"},
    ],
    ids=["no-secret-key", "secret-key-and-old-flag", "dev-flag"],
)
def test_prod_starts_and_only_v2_logs_in(tmp_path, extra):
    cookies = {
        "anonymous": None,
        "old_cookie": OLD_COOKIE,
        "v2": v2_token({"email": "journalist@example.no"}),
    }
    env = prod_env(tmp_path, EVALS_API_KEY="real-evals-key", **extra)

    result = run(PROBE, env, json.dumps(cookies))

    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "anonymous": 302,
        "old_cookie": 302,
        "v2": 200,
    }


@pytest.mark.parametrize(
    "extra",
    [{}, {"KLADD_DEV": "1"}, {"EVALS_API_KEY": DEV_API_KEY}],
    ids=["missing", "missing-with-dev-flag", "dev-default-value"],
)
def test_prod_refuses_to_start_without_a_real_api_key(tmp_path, extra):
    result = run("import kladd.frontend.app", prod_env(tmp_path, **extra))

    assert result.returncode != 0
    assert "EVALS_API_KEY is required" in result.stderr


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"KLADD_DEV": "1"}, "dev-default"),
        ({"KLADD_DEV": "1", "X_TEST_KEY": "real"}, "real"),
        ({"X_TEST_KEY": "real"}, "real"),
        ({"DOKKU_APP_TYPE": "dockerfile", "X_TEST_KEY": "real"}, "real"),
        ({"DOKKU_APP_NAME": "sitatkladd", "X_TEST_KEY": "real"}, "real"),
    ],
    ids=["local-dev", "local-dev-set", "local-set", "dokku-set", "dokku-name-set"],
)
def test_require_env_returns(monkeypatch, env, expected):
    for name in ("KLADD_DEV", "DOKKU_APP_TYPE", "DOKKU_APP_NAME", "X_TEST_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert require_env("X_TEST_KEY", dev_default="dev-default") == expected


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"DOKKU_APP_TYPE": "dockerfile", "KLADD_DEV": "1"},
        {"DOKKU_APP_NAME": "sitatkladd", "KLADD_DEV": "1"},
        {"DOKKU_APP_TYPE": "dockerfile", "KLADD_DEV": "1", "X_TEST_KEY": "dev-default"},
    ],
    ids=[
        "local-no-flag",
        "dokku-dev-flag",
        "dokku-name-dev-flag",
        "dokku-dev-default-value",
    ],
)
def test_require_env_raises(monkeypatch, env):
    for name in ("KLADD_DEV", "DOKKU_APP_TYPE", "DOKKU_APP_NAME", "X_TEST_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match="X_TEST_KEY is required"):
        require_env("X_TEST_KEY", dev_default="dev-default")

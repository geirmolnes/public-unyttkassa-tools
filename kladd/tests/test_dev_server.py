"""scripts/dev_server.py: a local run logs itself in, and it never starts on Dokku."""

import os

import pytest

from .test_prod_startup import REPO, run

# Loads the launcher without starting uvicorn and walks the login redirect.
PROBE = """
import runpy
from fastapi.testclient import TestClient

dev_app = runpy.run_path("scripts/dev_server.py")["dev_app"]
client = TestClient(dev_app, follow_redirects=False)
first = client.get("/")
print(first.status_code, first.headers["location"], client.get("/", follow_redirects=True).status_code)
login = client.get("/dev-login")
print(login.status_code, client.get("/").status_code, client.get("/rules").status_code)
"""

_SCRUBBED = (
    "DOKKU_APP_TYPE",
    "DOKKU_APP_NAME",
    "LOGIN_URL",
    "KLADD_ADMIN_EMAILS",
    "DEV_EMAIL",
)


def local_env(tmp_path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    return {**env, "KLADD_DATA_DIR": str(tmp_path), "PYTHONPATH": str(REPO), **extra}


def test_a_local_run_is_sent_to_dev_login_and_comes_back_signed_in(tmp_path):
    result = run(PROBE, local_env(tmp_path))

    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-2:] == [
        "302 /dev-login 200",
        "303 200 200",
    ]


@pytest.mark.parametrize("flag", ["DOKKU_APP_TYPE", "DOKKU_APP_NAME"])
def test_the_launcher_refuses_to_start_on_dokku(tmp_path, flag):
    result = run(PROBE, local_env(tmp_path, **{flag: "x"}))

    assert result.returncode != 0
    assert "local work only" in result.stderr

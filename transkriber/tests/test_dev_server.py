"""scripts/dev_server.py: a local run logs itself in, and it never starts on Dokku."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent

# Loads the launcher without starting uvicorn and walks the login redirect.
PROBE = """
import runpy
import sys
from fastapi.testclient import TestClient

dev_app = runpy.run_path(sys.argv[1])["dev_app"]
client = TestClient(dev_app, follow_redirects=False)
first = client.get("/")
print(first.status_code, first.headers["location"])
print(client.get("/dev-login").status_code, client.get("/").status_code)
"""

_SCRUBBED = ("DOKKU_APP_TYPE", "DOKKU_APP_NAME", "LOGIN_URL", "LOGOUT_URL", "DEV_EMAIL")


def _run(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    env |= {
        "UPLOAD_DIR": str(tmp_path),
        "TRANSKRIBER_DB": str(tmp_path / "transkriber.db"),
        **extra,
    }
    env.pop("PYTHONPATH", None)
    # Run from outside the repo: the launcher has to find `src` on its own, as it must
    # when started with `python scripts/dev_server.py`.
    return subprocess.run(
        [sys.executable, "-c", PROBE, str(REPO / "scripts" / "dev_server.py")],
        check=False,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_local_run_is_sent_to_dev_login_and_comes_back_signed_in(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-2:] == ["302 /dev-login", "303 200"]


@pytest.mark.parametrize("flag", ["DOKKU_APP_TYPE", "DOKKU_APP_NAME"])
def test_the_launcher_refuses_to_start_on_dokku(tmp_path, flag):
    result = _run(tmp_path, **{flag: "x"})

    assert result.returncode != 0
    assert "local work only" in result.stderr

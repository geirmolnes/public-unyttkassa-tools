"""Every HTML page renders for a signed-in user."""

import os

os.environ.setdefault("EVALS_API_KEY", "test-evals-key")

import pytest
from fastapi.testclient import TestClient

from kladd.frontend.app import app

from .conftest import v2_token


@pytest.mark.parametrize(
    "path,expected_status",
    [("/", 200), ("/profiles", 200), ("/drafts", 200), ("/drafts/999999", 404)],
    ids=["index", "profiles", "drafts", "missing-draft"],
)
def test_page_renders(tmp_db, path, expected_status):
    # starlette 1.0 dropped TemplateResponse(name, {"request": ...}); the old call
    # signature 500s every page, so render them all.
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("session", v2_token({"email": "a@x.com"}))

    response = client.get(path)

    assert response.status_code == expected_status
    assert response.headers["content-type"] == "text/html; charset=utf-8"

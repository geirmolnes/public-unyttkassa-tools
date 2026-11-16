"""Tests for admin-managed standing rules: storage, prompt, routes."""

import os
from types import SimpleNamespace

os.environ.setdefault("EVALS_API_KEY", "test-evals-key")

import pytest
from fastapi.testclient import TestClient

import kladd.storage as storage_mod
from kladd.frontend.app import app
from kladd.prompt import build_user_prompt
from kladd.storage import add_rule, delete_rule, list_rules, rules_for_user

from .conftest import make_source, v2_token

ADMIN = "admin@example.org"


def _client(email: str) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("session", v2_token({"email": email}))
    return client


@pytest.mark.parametrize(
    "user,expected",
    (
        ("a@x.com", ["global 1", "global 2", "a only"]),
        ("A@X.com", ["global 1", "global 2", "a only"]),
        ("b@x.com", ["global 1", "global 2", "b only"]),
        ("c@x.com", ["global 1", "global 2"]),
        (None, ["global 1", "global 2"]),
    ),
    ids=["scoped", "case-insensitive", "other-user", "no-scoped-rules", "no-user"],
)
def test_rules_for_user(tmp_db, user, expected):
    # Scoped rule inserted between globals: globals must still come first.
    add_rule("global 1")
    add_rule("a only", user_email="a@x.com")
    add_rule("global 2", user_email="")
    add_rule("b only", user_email="b@x.com")
    assert rules_for_user(user) == expected


@pytest.mark.parametrize(
    "user_email,expected_scope",
    (
        (None, None),
        ("", None),
        ("   ", None),
        ("  A@X.com ", "a@x.com"),
    ),
    ids=["none", "empty", "whitespace", "trimmed-lowercased"],
)
def test_add_rule_scope(tmp_db, user_email, expected_scope):
    add_rule("  Regel  ", user_email=user_email)
    [rule] = list_rules()
    assert (rule["text"], rule["user_email"]) == ("Regel", expected_scope)


@pytest.mark.parametrize("text", ("", "   ", "\n"), ids=["empty", "spaces", "newline"])
def test_add_rule_rejects_blank(tmp_db, text):
    with pytest.raises(ValueError):
        add_rule(text)


def test_delete_rule(tmp_db):
    keep = add_rule("keep")
    drop = add_rule("drop")
    delete_rule(drop)
    assert [r["id"] for r in list_rules()] == [keep]


@pytest.mark.parametrize(
    "rules,expected_block",
    (
        (None, None),
        ([], None),
        (["  "], None),
        (["Én regel"], "- Én regel"),
        ([" a ", "", "b"], "- a\n- b"),
    ),
    ids=["none", "empty-list", "blank-only", "one", "trim-and-skip-blank"],
)
def test_prompt_rules_block(rules, expected_block):
    result = build_user_prompt("sources", 1, rules=rules)
    if expected_block is None:
        assert "Faste regler" not in result
        return
    assert f"):\n{expected_block}\n\nsources" in result


def test_prompt_rules_before_instructions():
    result = build_user_prompt("sources", 1, rules=["r"], instructions="i")
    assert result.index("Faste regler") < result.index("Instruks fra journalisten")


@pytest.mark.parametrize(
    "method,path",
    (("get", "/rules"), ("post", "/rules"), ("post", "/rules/1/delete")),
)
def test_rules_routes_hidden_from_non_admin(tmp_db, method, path):
    add_rule("x")
    client = _client("someone@x.com")
    resp = client.get(path) if method == "get" else client.post(path, data={"text": "y"})
    assert resp.status_code == 404
    assert [r["text"] for r in list_rules()] == ["x"]


def test_admin_adds_and_deletes_rule(tmp_db):
    client = _client(ADMIN)
    resp = client.post("/rules", data={"text": "Bruk «statsråd»", "user_email": "a@x.com"})
    assert (resp.status_code, resp.headers["location"]) == (303, "/rules")
    [rule] = list_rules()
    assert (rule["text"], rule["user_email"]) == ("Bruk «statsråd»", "a@x.com")

    page = client.get("/rules")
    assert page.status_code == 200
    assert "Bruk «statsråd»" in page.text and "a@x.com" in page.text

    client.post(f"/rules/{rule['id']}/delete")
    assert list_rules() == []


@pytest.mark.parametrize(
    "email,link_shown", ((ADMIN, True), ("someone@x.com", False)), ids=["admin", "user"]
)
def test_nav_link_only_for_admin(tmp_db, email, link_shown):
    assert ('href="/rules"' in _client(email).get("/").text) is link_shown


@pytest.mark.parametrize(
    "user,expected_rules",
    (("a@x.com", ["global", "a only"]), ("b@x.com", ["global"])),
    ids=["scoped-user", "other-user"],
)
def test_draft_stream_passes_user_rules(tmp_db, monkeypatch, user, expected_rules):
    """The rules reaching the drafter are the logged-in user's, not anyone else's."""
    import kladd.drafter
    import kladd.quote_check
    import kladd.sources

    monkeypatch.setattr(storage_mod, "PROFILE_LOG", tmp_db / "profiling_log.md")
    add_rule("global")
    add_rule("a only", user_email="a@x.com")

    seen: dict = {}

    async def fake_stream(*args, **kwargs):
        seen.update(kwargs)
        yield "# Tittel"

    async def fake_verify(text, parsed, **kwargs):
        return text, True, None, []

    loaded = SimpleNamespace(
        parsed=[make_source()], dropped=[], formatted="src", count=1, all_urls=True
    )
    monkeypatch.setattr(kladd.drafter, "stream_draft_article", fake_stream)
    monkeypatch.setattr(kladd.sources, "load_and_format", lambda sources: loaded)
    monkeypatch.setattr(kladd.quote_check, "verify_and_apply", fake_verify)

    resp = _client(user).post("/draft-stream", data={"urls": "https://example.com/a"})
    assert "event: done" in resp.text
    assert seen["rules"] == expected_rules

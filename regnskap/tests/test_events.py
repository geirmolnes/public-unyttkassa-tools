"""The event log behind /api/diagnostics, and resuming reads that a stopped process (a deploy) left behind."""

import logging

import pytest

from src import events
from src.accounts import pipeline, store
from src.main import app
from src.routes import api
from tests.conftest import TEST_EMAIL, session_cookie
from tests.test_accounts import ORGNR, PDF, needs_filing, seed_reads, wait

OLD = "oldrev:oldcontainer:7"
ADMIN = {"Cookie": f"session={session_cookie('admin@example.com')}"}
LONG_AGO = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def restaurant(brreg):
    brreg.company(ORGNR, "18 RESTAURANT AS", [2024, 2025])
    brreg.pdf_bytes[(ORGNR, 2025)] = PDF


@pytest.fixture
def reading_ctx():
    token = events.current.set((None, None))
    yield
    events.current.reset(token)


def stopped_read(heartbeat: str | None = LONG_AGO, attempts: int = 0) -> None:
    """A read another process was doing when it stopped."""
    store.queue(ORGNR, 2025, "fake/model", "18 RESTAURANT AS", OLD)
    store.update(
        ORGNR, 2025, status="running", phase="leser", heartbeat_at=heartbeat, attempts=attempts
    )


def resume(app_client) -> list:
    async def go() -> list:
        return pipeline.resume_stale(app.state.brreg, app.state.reader, api.background)

    return app_client.portal.call(go)


# ---------- the log ----------


def test_log_warnings_become_events_tagged_with_the_read(reading_ctx):
    events.install_log_handler()
    events.about("123456789", 2022)
    logging.getLogger("src.accounts.llm").warning("page read retry %s", "429")
    e = events.recent(kind="log")[0]
    assert (e["kind"], e["level"], e["orgnr"], e["year"], e["message"]) == (
        "log.accounts.llm",
        "warning",
        "123456789",
        2022,
        "page read retry 429",
    )


def test_exceptions_keep_their_traceback():
    try:
        raise ValueError("boom")
    except ValueError as e:
        events.record("x.failed", "m", level="error", exc=e, detail={"page": 3})
    d = events.recent()[0]["detail"]
    assert (d["page"], "ValueError: boom" in d["traceback"]) == (3, True)


def test_recent_filters_by_kind_prefix_and_problem_level():
    events.record("read.start", "a")
    events.record("read.failed", "b", level="error")
    events.record("reader.x", "c", level="warning")
    assert [e["message"] for e in events.recent(kind="read")] == ["b", "a"]
    assert [e["message"] for e in events.recent(level="warning")] == ["c", "b"]


@needs_filing
def test_a_read_records_each_step(app_client, restaurant):
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    kinds = [e["kind"] for e in reversed(events.recent(orgnr=ORGNR))]
    assert kinds == ["read.requested", "read.start", "read.pdf", "read.phase", "read.done"]


@needs_filing
def test_a_failed_read_leaves_its_traceback_in_diagnostics(app_client, restaurant, reader):
    async def broken(*a, **k):
        raise RuntimeError("model down")

    reader.read = broken
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    assert wait(app_client)["status"] == "failed"
    d = app_client.get(f"/api/diagnostics?orgnr={ORGNR}&level=warning", headers=ADMIN).json()
    tb = next(e for e in d["events"] if e["kind"] == "log.accounts.pipeline")
    assert (tb["year"], "RuntimeError: model down" in tb["detail"]["traceback"]) == (2025, True)
    assert [e["kind"] for e in d["events"] if e["kind"].startswith("read.")] == ["read.failed"]


def test_unhandled_server_errors_are_recorded(app_client, restaurant, reader):
    def chain():
        raise RuntimeError("chain broke")

    reader.chain = chain
    with pytest.raises(RuntimeError):
        app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    e = events.recent(kind="http")[0]
    assert (e["kind"], e["message"], "chain broke" in e["detail"]["traceback"]) == (
        "http.exception",
        f"POST /api/company/{ORGNR}/accounts",
        True,
    )


def test_browser_errors_are_recorded_with_the_user(app_client):
    r = app_client.post(
        "/api/client-log",
        json={"kind": "api", "message": "POST /x → 502: nede", "view": "company", "orgnr": ORGNR},
    )
    e = events.recent(kind="client")[0]
    assert (r.status_code, e["kind"], e["orgnr"], e["detail"]["user"]) == (
        204,
        "client.api",
        ORGNR,
        TEST_EMAIL,
    )


def test_diagnostics_lists_recent_reads_and_the_release(app_client):
    stopped_read()
    d = app_client.get("/api/diagnostics", headers=ADMIN).json()
    assert (d["release"], [(r["orgnr"], r["year"], r["owner"]) for r in d["reads"]]) == (
        events.RELEASE,
        [(ORGNR, 2025, OLD)],
    )
    assert "app.start" in [e["kind"] for e in d["events"]]


def test_diagnostics_are_for_the_admin_only(app_client):
    """It shows who read which company; colleagues must not see that."""
    r = app_client.get("/api/diagnostics")
    assert (r.status_code, TEST_EMAIL in r.json()["detail"]) == (403, True)


# ---------- resuming ----------


@pytest.mark.parametrize("heartbeat", [LONG_AGO, None], ids=["silent", "never-vouched"])
@needs_filing
def test_a_read_stopped_by_a_deploy_is_resumed(app_client, restaurant, reader, heartbeat):
    """Regression: a deploy mid-read left "Avbrutt av omstart. Prøv igjen." (Læringsverkstedet 2023, 2026-10-12)."""
    seed_reads()
    stopped_read(heartbeat)
    assert resume(app_client) == [(ORGNR, 2025)]
    row = wait(app_client)
    assert (row["status"], reader.calls) == ("ok", [])
    e = events.recent(orgnr=ORGNR, kind="read.resumed")[0]
    assert (e["detail"]["owner"], e["detail"]["phase"]) == (OLD, "leser")


def test_a_read_its_process_still_vouches_for_is_left_alone(app_client, restaurant):
    stopped_read(heartbeat=None)
    store.heartbeat(OLD)
    assert (resume(app_client), store.account_read(ORGNR, 2025)["owner"]) == ([], OLD)


def test_a_read_that_keeps_stopping_fails(app_client, restaurant):
    stopped_read(attempts=pipeline.MAX_ATTEMPTS)
    assert resume(app_client) == []
    row = store.account_read(ORGNR, 2025)
    assert (row["status"], row["error"]) == ("failed", pipeline.GAVE_UP)
    assert events.recent(orgnr=ORGNR)[0]["kind"] == "read.gave_up"


def test_only_one_process_can_take_over_a_read():
    stopped_read()
    assert [store.claim(ORGNR, 2025, p, LONG_AGO) for p in ("a", "b")] == [True, False]


def test_a_new_click_resets_the_takeover_count():
    stopped_read(attempts=2)
    store.queue(ORGNR, 2025, "fake/model", "18 RESTAURANT AS", events.PROCESS)
    assert store.account_read(ORGNR, 2025)["attempts"] == 0

"""Per-user limits on paid and log-writing endpoints and the daily read budget."""

from datetime import UTC, datetime, timedelta

import pytest

from src import db, events, jobs, limits
from src.accounts import store
from tests.conftest import TEST_EMAIL, session_cookie
from tests.test_accounts import ORGNR, PDF

ADMIN_EMAIL = "admin@example.com"
OTHER = "998812623"


@pytest.fixture
def restaurant(brreg):
    brreg.company(ORGNR, "18 RESTAURANT AS", [2024, 2025])
    brreg.pdf_bytes[(ORGNR, 2025)] = PDF


def _read(client, cookie_email: str = TEST_EMAIL) -> int:
    resp = client.post(
        f"/api/company/{ORGNR}/accounts",
        json={"years": [2025]},
        headers={"Cookie": f"session={session_cookie(cookie_email)}"},
    )
    return resp.status_code


# ---------- the window itself ----------


@pytest.mark.parametrize(
    "used,n,allowed",
    [(0, 1, True), (2, 1, True), (3, 1, False), (0, 3, True), (0, 4, False), (1, 3, False)],
    ids=["first", "last-unit", "one-over", "all-at-once", "more-than-limit", "would-overshoot"],
)
def test_window_allows_up_to_the_limit(used, n, allowed):
    w = limits.Window(3, 60)
    for _ in range(used):
        w.take("a", now=0)
    assert w.take("a", n, now=0) is allowed


@pytest.mark.parametrize(
    "now,allowed", [(59.9, False), (60.0, True), (61.0, True)], ids=["inside", "edge", "after"]
)
def test_window_frees_units_after_its_length(now, allowed):
    w = limits.Window(1, 60)
    w.take("a", now=0)
    assert w.take("a", now=now) is allowed


def test_window_counts_each_key_separately():
    w = limits.Window(1, 60)
    w.take("a", now=0)
    assert w.take("b", now=0) is True


def test_a_refused_take_spends_nothing():
    w = limits.Window(3, 60)
    w.take("a", 2, now=0)
    w.take("a", 2, now=0)
    assert w.used("a", now=0) == 2


# ---------- reads ----------


@pytest.mark.parametrize("used,status", [(58, 202), (59, 202), (60, 429)])
def test_read_years_per_user_per_day(app_client, restaurant, used, status):
    limits.READ_YEARS.take(TEST_EMAIL, used)
    assert _read(app_client) == status


def test_read_limit_has_a_norwegian_reason(app_client, restaurant):
    limits.READ_YEARS.take(TEST_EMAIL, 60)
    resp = app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    assert (
        resp.json()["detail"] == "Grensen er 60 innleste år per døgn per bruker. Prøv igjen senere."
    )


def test_admins_are_not_limited_per_user(app_client, restaurant):
    limits.READ_YEARS.take(ADMIN_EMAIL, 60)
    assert _read(app_client, ADMIN_EMAIL) == 202


def _spent(cost: float, started_at: str | None = None) -> None:
    store.queue(OTHER, 2020, "fake/model", "HJR HOLDING AS")
    store.update(OTHER, 2020, status="ok", cost=cost)
    if started_at:
        store.update(OTHER, 2020, started_at=started_at)


@pytest.mark.parametrize("cost,status", [(19.99, 202), (20.0, 429), (20.01, 429)])
def test_daily_read_budget(app_client, restaurant, cost, status):
    _spent(cost)
    assert _read(app_client) == status


def test_daily_read_budget_binds_admins_too(app_client, restaurant):
    _spent(20.0)
    assert _read(app_client, ADMIN_EMAIL) == 429


def test_yesterdays_spend_does_not_count(app_client, restaurant):
    yesterday = (datetime.now(UTC) - timedelta(days=1)).replace(hour=12).isoformat()
    _spent(500.0, started_at=yesterday)
    assert _read(app_client) == 202


# ---------- subsidiaries "Les på nytt" ----------


def _extracted(brreg, status: str = "ok") -> None:
    brreg.company(ORGNR, "18 RESTAURANT AS", [2024, 2025])
    db.start_extraction(ORGNR, 2025, "fake")
    db.finish_extraction(ORGNR, 2025, status, n_rows=0)


@pytest.mark.parametrize("used,status", [(19, 202), (20, 429)])
def test_subsidiary_reruns_per_user_per_day(app_client, brreg, used, status):
    _extracted(brreg)
    limits.SUBSIDIARY_RERUNS.take(TEST_EMAIL, used)
    resp = app_client.post(f"/api/company/{ORGNR}/subsidiaries", params={"force": "true"})
    assert resp.status_code == status


@pytest.mark.parametrize("force", [False, True], ids=["no-force", "retry-after-failure"])
def test_subsidiary_starts_that_are_not_reruns_are_not_counted(app_client, brreg, force):
    _extracted(brreg, "failed" if force else "ok")
    app_client.post(f"/api/company/{ORGNR}/subsidiaries", params={"force": str(force).lower()})
    assert limits.SUBSIDIARY_RERUNS.used(TEST_EMAIL) == 0


# ---------- chat ----------


def _chat(client) -> int:
    resp = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hei"}]})
    return resp.status_code


@pytest.mark.parametrize("used,status", [(59, 200), (60, 429)])
def test_chat_messages_per_user_per_hour(app_client, used, status):
    limits.CHAT.take(TEST_EMAIL, used)
    assert _chat(app_client) == status


# ---------- client log ----------


def _log(client, **body) -> int:
    resp = client.post("/api/client-log", json={"kind": "js", "message": "boom", **body})
    return resp.status_code


@pytest.mark.parametrize("used,stored", [(29, 1), (30, 0)])
def test_client_log_drops_events_over_the_limit(app_client, used, stored):
    limits.CLIENT_LOG.take(TEST_EMAIL, used)
    assert (_log(app_client), len(events.recent(kind="client"))) == (204, stored)


@pytest.mark.parametrize("kind", ["js", "promise", "network", "api", "shown"])
def test_client_log_accepts_the_kinds_the_frontend_sends(app_client, kind):
    assert _log(app_client, kind=kind) == 204


@pytest.mark.parametrize("kind", ["", "x", "log.src.auth", "js "])
def test_client_log_rejects_other_kinds(app_client, kind):
    assert _log(app_client, kind=kind) == 422


@pytest.mark.parametrize(
    "url,stored",
    [
        (
            "https://regnskap.example.org/?regnskap=921830327&aar=2024",
            "/?regnskap=921830327&aar=2024",
        ),
        ("https://regnskap.example.org/?eier=Kari%20Nordmann&f=1970", "/"),
        (
            "https://regnskap.example.org/?eierstruktur=Kari+Nordmann&f=1970&aar=2024",
            "/?aar=2024",
        ),
        ("https://regnskap.example.org/?personer=kari&orgnr=921830327", "/?orgnr=921830327"),
        ("https://regnskap.example.org/", "/"),
        (None, None),
    ],
    ids=["company", "person", "person-structure", "people-search", "bare", "missing"],
)
def test_client_log_keeps_no_person_names_from_the_url(app_client, url, stored):
    _log(app_client, url=url)
    assert events.recent(kind="client")[0]["detail"]["url"] == stored


# ---------- ZIP jobs ----------


def _active(owner: str, n: int, phase: str = "downloading") -> None:
    for _ in range(n):
        job = jobs.create_job(owner, [ORGNR], jobs.Selection("all"), batch=True)
        job.phase = phase


def _job(client) -> int:
    return client.post("/api/jobs", json={"orgnrs": ORGNR, "mode": "all"}).status_code


@pytest.mark.parametrize("running,status", [(2, 200), (3, 429)])
def test_active_jobs_per_user(app_client, brreg, running, status):
    _active(TEST_EMAIL, running)
    assert _job(app_client) == status


@pytest.mark.parametrize("running,status", [(9, 200), (10, 429)])
def test_active_jobs_in_total(app_client, brreg, running, status):
    _active("someone@example.com", running)
    assert _job(app_client) == status


@pytest.mark.parametrize("phase", ["done", "failed"])
def test_finished_jobs_do_not_count(app_client, brreg, phase):
    _active(TEST_EMAIL, 3, phase)
    assert _job(app_client) == 200


@pytest.mark.parametrize(
    "n_invalid,expected",
    [
        (1, (1, None)),
        (200, (200, None)),
        (201, (201, "og 1 linjer til som ikke er organisasjonsnummer")),
        (500, (201, "og 300 linjer til som ikke er organisasjonsnummer")),
    ],
    ids=["one", "at-cap", "one-over", "many"],
)
def test_invalid_lines_reported_are_capped(app_client, brreg, n_invalid, expected):
    text = "\n".join([ORGNR] + [f"x{i}" for i in range(n_invalid)])
    failures = app_client.post("/api/jobs", json={"orgnrs": text, "mode": "all"}).json()["failures"]
    invalid = [f for f in failures if f["orgnr"] != ORGNR]
    summary = invalid[-1]["reason"] if invalid[-1]["orgnr"] == "…" else None
    assert (len(invalid), summary) == expected

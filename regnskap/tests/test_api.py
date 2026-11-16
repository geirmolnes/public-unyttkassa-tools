"""HTTP routes: auth, search, company, PDF download and ZIP jobs (Brreg mocked)."""

import asyncio
import importlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import auth, db, events
from src.routes import api
from tests.conftest import session_cookie, signed_claims


def _wait(app_client, job_id: str) -> dict:
    for _ in range(200):
        data = app_client.get(f"/api/jobs/{job_id}").json()
        if data["phase"] in ("done", "failed"):
            return data
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_health_is_public():
    from src.main import app

    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "cookie",
    [None, "garbage", signed_claims({"user_id": 1, "email": ""}), signed_claims({"user_id": 1})],
    ids=["missing", "forged", "empty-email", "no-email"],
)
def test_unauthenticated_redirects_to_login(cookie):
    from src.main import app

    headers = {"Cookie": f"session={cookie}"} if cookie else {}
    with TestClient(app, headers=headers, follow_redirects=False) as c:
        resp = c.get("/api/search?q=hjr")
    assert (resp.status_code, resp.headers["location"]) == (302, "https://example.org/login")


@pytest.mark.parametrize(
    ("env", "login", "logout"),
    [
        ({}, "https://example.org/login", "https://example.org/logout"),
        ({"LOGIN_URL": "/inn", "LOGOUT_URL": "/ut"}, "/inn", "/ut"),
    ],
    ids=["default", "overridden"],
)
def test_login_and_logout_urls_come_from_the_environment(
    app_client, monkeypatch, env, login, logout
):
    try:
        # A context of its own: monkeypatch.undo() here would also undo the data dir set for the test.
        with monkeypatch.context() as m:
            for name in ("LOGIN_URL", "LOGOUT_URL"):
                m.delenv(name, raising=False)
            for name, value in env.items():
                m.setenv(name, value)
            importlib.reload(auth)
            anonymous = TestClient(app_client.app, follow_redirects=False).get("/")
            assert (anonymous.status_code, anonymous.headers["location"]) == (302, login)
            assert f'<form action="{logout}" method="post">' in app_client.get("/").text
    finally:
        importlib.reload(auth)


def test_index_renders(app_client):
    resp = app_client.get("/?orgnr=998812623")
    assert resp.status_code == 200 and 'id="search-input"' in resp.text


def test_index_is_never_stored(app_client):
    """Regression: Safari restored a tab from a cached page of an older release, and its `app.js?v=old` got
    today's app.js (StaticFiles ignores ?v=), which crashed on an element the old page lacked. Safari keeps
    and reuses pages for history/tab restore without revalidating even under no-cache; only no-store stops it."""
    assert app_client.get("/").headers.get("cache-control") == "no-store"


@pytest.mark.parametrize("q", ["", "h", " h "], ids=["empty", "1-char", "1-char-padded"])
def test_search_too_short_skips_brreg(app_client, q):
    assert app_client.get("/api/search", params={"q": q, "people": "true"}).json() == {
        "results": [],
        "people": [],
    }


def test_search_by_name_returns_disambiguating_fields(app_client, brreg):
    brreg.search_rows = [
        {
            "organisasjonsnummer": "998812623",
            "navn": "HJR HOLDING AS",
            "konkurs": True,
            "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
            "forretningsadresse": {"poststed": "JESSHEIM"},
        }
    ]
    [r] = app_client.get("/api/search", params={"q": "hjr"}).json()["results"]
    assert (r["orgnr"], r["form_code"], r["place"], r["konkurs"], r["kind"]) == (
        "998812623",
        "AS",
        "JESSHEIM",
        True,
        "enhet",
    )


@pytest.mark.parametrize(
    "orgnr,status,detail",
    [
        ("12345", 400, "Organisasjonsnummer må ha 9 siffer (fikk 5)"),
        ("123456789", 400, "Ugyldig organisasjonsnummer (feil kontrollsiffer)"),
        ("998812623", 404, "Fant ikke 998812623 i Enhetsregisteret."),
    ],
    ids=["too-short", "bad-checksum", "unknown"],
)
def test_company_errors(app_client, brreg, orgnr, status, detail):
    resp = app_client.get(f"/api/company/{orgnr}")
    assert (resp.status_code, resp.json()["detail"]) == (status, detail)


def test_company_returns_years_and_figures(app_client, brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2025, 2024])
    brreg.figures["998812623"] = [
        {"regnskapstype": "SELSKAP", "resultatregnskapResultat": {"aarsresultat": 1.0}}
    ]
    body = app_client.get("/api/company/998812623").json()
    assert (body["years"], body["key_figures"]["aarsresultat"]) == ([2024, 2025], 1.0)


def test_company_returns_newest_year_and_history(app_client, brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2023, 2024, 2025])
    brreg.figures["998812623"] = json.loads(
        (Path(__file__).parent / "fixtures" / "hjr_regnskap_2023_2025.json").read_text()
    )
    body = app_client.get("/api/company/998812623").json()
    assert body["key_figures"]["year"] == 2025
    assert [kf["year"] for kf in body["key_figures_history"]] == [2025, 2024, 2023]
    assert [kf["year"] for kf in body["key_figures_konsern_history"]] == [2025, 2024, 2023]


def test_company_without_group_accounts_has_empty_konsern(app_client, brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2025])
    brreg.figures["998812623"] = [{"regnskapstype": "SELSKAP"}]
    assert app_client.get("/api/company/998812623").json()["key_figures_konsern_history"] == []


def test_pdf_download_has_company_year_filename(app_client, brreg):
    brreg.company("926052586", "ÆØÅ AS", [2024])
    resp = app_client.get("/api/pdf/926052586/2024")
    assert (
        resp.status_code,
        resp.headers["content-type"],
        resp.headers["content-disposition"],
    ) == (
        200,
        "application/pdf",
        "attachment; filename=\"AEOAA_AS_2024.pdf\"; filename*=UTF-8''%C3%86%C3%98%C3%85_AS_2024.pdf",
    )


def test_pdf_missing_year_is_404_in_norwegian(app_client, brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2024])
    resp = app_client.get("/api/pdf/998812623/1999")
    assert (resp.status_code, resp.json()["detail"]) == (
        404,
        "Brreg har ingen kopi av årsregnskapet for 1999",
    )


def test_brreg_down_is_502(app_client, brreg):
    import httpx

    brreg.queued = [httpx.Response(503)] * 12  # 4 concurrent lookups × 3 attempts
    resp = app_client.get("/api/company/998812623")
    assert (resp.status_code, resp.json()["detail"]) == (
        502,
        "Brønnøysundregistrene svarer ikke akkurat nå. Prøv igjen om litt.",
    )


def test_company_lookups_run_at_once(app_client, brreg, monkeypatch):
    """Regression: roles, then each owner, were looked up one after another after the rest, so a
    cold open waited 2 + owners round trips to Brreg instead of one."""
    brreg.company("998831067", "DIBBER AS", [2025])
    for parent in ("998812623", "921830327"):
        brreg.company(parent, "MOR AS", [2025])
        db.replace_ownership(parent, 2025, [{"name": "Dibber AS", "orgnr": "998831067"}], "m")
    http = app_client.app.state.brreg.http
    real_get = http.get

    async def slow_get(url, **kw):
        await asyncio.sleep(0.25)
        return await real_get(url, **kw)

    monkeypatch.setattr(http, "get", slow_get)
    start = time.perf_counter()
    body = app_client.get("/api/company/998831067").json()
    elapsed = time.perf_counter() - start
    assert [o["name"] for o in body["ownership"]["owned_by"]] == ["MOR AS", "MOR AS"]
    # One round trip is 0.25 s; one after another it was 1.0 s (lookups, roles, 2 owners)
    assert elapsed < 0.5, f"{elapsed:.2f} s"


def test_bank_opens_without_brreg_figures(app_client, brreg):
    """Regression: Brreg's /regnskap answers banks with a 500 every time; the page retried for 3 s,
    then said Brreg was down. Now one request, and the page shows the rest."""
    brreg.company("984851006", "DNB BANK ASA", [2024, 2025])
    brreg.banks.add("984851006")
    body = app_client.get("/api/company/984851006").json()
    assert (
        body["company"]["name"],
        body["years"],
        body["key_figures"],
        brreg.paths.count("/regnskapsregisteret/regnskap/984851006"),
    ) == ("DNB BANK ASA", [2024, 2025], None, 1)


@pytest.mark.parametrize("threshold,recorded", [(0.0, 1), (60.0, 0)], ids=["slow", "fast"])
def test_slow_company_open_is_recorded_with_its_steps(
    app_client, brreg, monkeypatch, threshold, recorded
):
    monkeypatch.setattr(api, "SLOW_OPEN_S", threshold)
    brreg.company("998812623", "HJR HOLDING AS", [2025])
    app_client.get("/api/company/998812623")
    assert [
        (e["level"], e["orgnr"], sorted(e["detail"]["steps"]), e["detail"]["owners"])
        for e in events.recent(kind="company.slow")
    ] == [
        ("warning", "998812623", ["aar", "arkiv", "eiere", "enhet", "regnskap", "roller"], 0)
    ] * recorded


def test_job_end_to_end_zip_download(app_client, brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2023, 2024, 2025])
    job = app_client.post(
        "/api/jobs", json={"orgnrs": ["998812623"], "mode": "years", "years": [2023, 2024, 2025]}
    ).json()
    assert _wait(app_client, job["id"])["phase"] == "done"
    resp = app_client.get(f"/api/jobs/{job['id']}/download")
    names = sorted(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())
    assert (resp.headers["content-disposition"], names) == (
        "attachment; filename=\"HJR_HOLDING_AS_2023-2025.zip\"; filename*=UTF-8''HJR_HOLDING_AS_2023-2025.zip",
        ["HJR_HOLDING_AS_2023.pdf", "HJR_HOLDING_AS_2024.pdf", "HJR_HOLDING_AS_2025.pdf"],
    )


def test_job_reports_invalid_lines(app_client, brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2025])
    job = app_client.post(
        "/api/jobs", json={"orgnrs": "998812623\n12345", "mode": "all", "batch": True}
    ).json()
    data = _wait(app_client, job["id"])
    assert data["failures"] == [
        {
            "orgnr": "12345",
            "reason": "Organisasjonsnummer må ha 9 siffer (fikk 5)",
            "name": None,
            "year": None,
        }
    ]


def test_job_is_private_to_its_owner(app_client, brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2025])
    job = app_client.post("/api/jobs", json={"orgnrs": ["998812623"], "mode": "all"}).json()
    _wait(app_client, job["id"])
    other = {"Cookie": f"session={session_cookie('annen@example.com')}"}
    assert app_client.get(f"/api/jobs/{job['id']}/download", headers=other).status_code == 404


# Valid-checksum org numbers for the size-limit rows.
def _orgnrs(n: int) -> list[str]:
    from src.parsing import has_valid_checksum

    out, i = [], 900000000
    while len(out) < n:
        if has_valid_checksum(str(i)):
            out.append(str(i))
        i += 1
    return out


@pytest.mark.parametrize("n,status", [(49, 200), (50, 200), (51, 400)])
def test_job_max_orgnrs(app_client, brreg, n, status):
    resp = app_client.post("/api/jobs", json={"orgnrs": _orgnrs(n), "mode": "all", "batch": True})
    assert resp.status_code == status


@pytest.mark.parametrize(
    "payload,detail",
    [
        ({"orgnrs": "abc", "mode": "all"}, "Fant ingen gyldige organisasjonsnummer."),
        ({"orgnrs": "998812623", "mode": "years", "years": []}, "Velg minst ett år."),
        (
            {"orgnrs": "998812623", "mode": "range", "from_year": 2020, "to_year": 2019},
            "Fra-år kan ikke være etter til-år",
        ),
    ],
    ids=["no-valid-orgnr", "no-years", "reversed-range"],
)
def test_job_rejects_bad_requests(app_client, payload, detail):
    resp = app_client.post("/api/jobs", json=payload)
    assert (resp.status_code, resp.json()["detail"]) == (400, detail)


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"years": list(range(1900, 2100))}, 200),
        ({"years": list(range(1900, 2101))}, 422),
        ({"years": [1900, 2100]}, 200),
        ({"years": [1899]}, 422),
        ({"years": [2101]}, 422),
        ({"mode": "range", "from_year": 1899, "to_year": 20245}, 200),
        ({"orgnrs": "998812623 " + "x" * (50_000 - 10)}, 200),
        ({"orgnrs": "998812623 " + "x" * (50_001 - 10)}, 400),
        ({"orgnrs": ["998812623", "x" * (50_000 - 10)]}, 200),
        ({"orgnrs": ["998812623", "x," * 25_000]}, 400),
    ],
    ids=[
        "200-years",
        "201-years",
        "year-bounds",
        "year-below",
        "year-above",
        "range-typo-still-accepted",
        "text-at-cap",
        "text-over-cap",
        "list-at-cap",
        "list-item-with-commas-over-cap",
    ],
)
def test_job_request_is_bounded(app_client, brreg, payload, status):
    body = {"orgnrs": "998812623", "mode": "years", "years": [2024], **payload}
    assert app_client.post("/api/jobs", json=body).status_code == status


def test_long_orgnr_list_gets_a_norwegian_reason(app_client):
    resp = app_client.post("/api/jobs", json={"orgnrs": "x," * 25_001, "mode": "all"})
    assert (resp.status_code, resp.json()["detail"]) == (
        400,
        "For lang liste. Maks 50 selskaper per nedlasting.",
    )


def test_unknown_job_is_404(app_client):
    assert app_client.get("/api/jobs/nope").status_code == 404

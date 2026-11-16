"""What Brreg sent for an opened company is kept: accounts rows per year, JSON snapshots, every PDF fetched."""

from src import db
from src.accounts import store
from tests.test_accounts import PDF, needs_filing

ORGNR = "921830327"


def row(year: int, sum_eiendeler: int, kind: str = "SELSKAP") -> dict:
    return {
        "regnskapstype": kind,
        "valuta": "NOK",
        "regnskapsperiode": {"fraDato": f"{year}-01-01", "tilDato": f"{year}-12-31"},
        "eiendeler": {"sumEiendeler": sum_eiendeler},
    }


def company(app_client, brreg, rows):
    brreg.figures[ORGNR] = rows
    return app_client.get(f"/api/company/{ORGNR}").json()


def test_a_year_brreg_stops_serving_is_still_shown_marked_archived(app_client, brreg):
    brreg.company(ORGNR, "18 RESTAURANT AS", [2025])
    company(app_client, brreg, [row(2025, 3), row(2024, 2), row(2023, 1)])
    app_client.app.state.brreg._cache.clear()
    later = company(app_client, brreg, [row(2026, 4), row(2025, 3), row(2024, 2)])
    assert [
        (k["year"], k["sum_eiendeler"], k["archived"]) for k in later["key_figures_history"]
    ] == [
        (2026, 4, False),
        (2025, 3, False),
        (2024, 2, False),
        (2023, 1, True),
    ]


def test_brreg_correcting_a_year_replaces_it_and_keeps_both_versions(app_client, brreg):
    brreg.company(ORGNR, "18 RESTAURANT AS", [2025])
    company(app_client, brreg, [row(2025, 3)])
    app_client.app.state.brreg._cache.clear()
    later = company(app_client, brreg, [row(2025, 5)])
    versions = db._all(
        "SELECT data FROM brreg_snapshots WHERE orgnr = ? AND source = 'regnskap:SELSKAP:2025-12-31'",
        (ORGNR,),
    )
    assert ([k["sum_eiendeler"] for k in later["key_figures_history"]], len(versions)) == ([5], 2)


def test_unit_json_is_kept_once_per_distinct_content(app_client, brreg):
    brreg.company(ORGNR, "18 RESTAURANT AS", [2025])
    company(app_client, brreg, [])
    app_client.app.state.brreg._cache.clear()
    company(app_client, brreg, [])
    app_client.app.state.brreg._cache.clear()
    brreg.enheter[ORGNR]["navn"] = "18 RESTAURANT NY AS"
    company(app_client, brreg, [])
    names = db._all(
        "SELECT json_extract(data, '$.navn') AS n FROM brreg_snapshots WHERE orgnr = ? AND source = 'enhet' "
        "ORDER BY first_seen",
        (ORGNR,),
    )
    assert [r["n"] for r in names] == ["18 RESTAURANT AS", "18 RESTAURANT NY AS"]


@needs_filing
def test_a_downloaded_pdf_is_kept(app_client, brreg):
    brreg.company(ORGNR, "18 RESTAURANT AS", [2025])
    brreg.pdf_bytes[(ORGNR, 2025)] = PDF
    resp = app_client.get(f"/api/pdf/{ORGNR}/2025")
    kept = store.pdf_file(store.content_hash(PDF))
    assert (resp.status_code, kept["n_pages"], kept["abs_path"].read_bytes() == PDF) == (
        200,
        5,
        True,
    )


def test_a_pdf_that_wont_open_is_still_served(app_client, brreg):
    """The fake «%PDF» bytes can't be opened: nothing kept, the download still works."""
    brreg.company(ORGNR, "18 RESTAURANT AS", [2024])
    resp = app_client.get(f"/api/pdf/{ORGNR}/2024")
    assert (resp.status_code, db._all("SELECT * FROM pdf_files")) == (200, [])

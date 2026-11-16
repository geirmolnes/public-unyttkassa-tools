"""Favourites, collections and recent via HTTP (Brreg mocked)."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import session_cookie


@pytest.fixture
def companies(brreg):
    brreg.company(
        "998812623", "HJR HOLDING AS", [2024, 2025], forretningsadresse={"poststed": "JESSHEIM"}
    )
    brreg.company("923609016", "EQUINOR ASA", [2025])
    brreg.figures["998812623"] = [
        {
            "regnskapstype": "SELSKAP",
            "valuta": "NOK",
            "regnskapsperiode": {"tilDato": "2025-12-31"},
            "resultatregnskapResultat": {"aarsresultat": 25264000.0},
            "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 25.0}},
            "eiendeler": {"sumEiendeler": 100.0},
        }
    ]
    return brreg


def test_favourite_roundtrip_with_row_figures(app_client, companies):
    assert app_client.put("/api/favourites/998812623").json() == {"ok": True}
    app_client.put("/api/favourites/923609016")
    items = app_client.get("/api/favourites", params={"rows": "true"}).json()["items"]
    assert items == [
        {
            "orgnr": "998812623",
            "name": "HJR HOLDING AS",
            "place": "JESSHEIM",
            "last_pdf_year": 2025,
            "key_year": 2025,
            "currency": "NOK",
            "aarsresultat": 25264000.0,
            "ek_andel": 0.25,
        },
        {
            "orgnr": "923609016",
            "name": "EQUINOR ASA",
            "place": None,
            "last_pdf_year": 2025,
            "key_year": None,
            "currency": None,
            "aarsresultat": None,
            "ek_andel": None,
        },
    ]
    app_client.delete("/api/favourites/998812623")
    assert [i["orgnr"] for i in app_client.get("/api/favourites").json()["items"]] == ["923609016"]


def test_favourites_list_skips_brreg(app_client, companies, brreg):
    """Regression: the sidebar refreshes favourites on every company open, and each favourite cost
    three Brreg lookups, one on the accounts register's 30-per-window rate limit shared with PDF reads."""
    app_client.put("/api/favourites/998812623")
    app_client.put("/api/favourites/923609016")
    before = len(brreg.paths)
    items = app_client.get("/api/favourites").json()["items"]
    assert (items, brreg.paths[before:]) == (
        [
            {"orgnr": "998812623", "name": "HJR HOLDING AS"},
            {"orgnr": "923609016", "name": "EQUINOR ASA"},
        ],
        [],
    )


@pytest.mark.parametrize(
    "orgnr,status", [("998812623", 404), ("12345", 400)], ids=["unknown", "malformed"]
)
def test_favourite_rejects_bad_orgnr(app_client, brreg, orgnr, status):
    assert app_client.put(f"/api/favourites/{orgnr}").status_code == status


def test_collection_lifecycle(app_client, companies):
    cid = app_client.post(
        "/api/collections", json={"name": " Portefølje ", "orgnrs": ["998 812 623", "998812623"]}
    ).json()["id"]
    app_client.put(f"/api/collections/{cid}/members/923609016")
    app_client.patch(f"/api/collections/{cid}", json={"name": "Kunder"})
    body = app_client.get(f"/api/collections/{cid}").json()
    assert (body["name"], [i["orgnr"] for i in body["items"]]) == (
        "Kunder",
        ["998812623", "923609016"],
    )
    assert app_client.get("/api/collections").json()["items"] == [
        {"id": cid, "name": "Kunder", "count": 2, "preview": ["HJR HOLDING AS", "EQUINOR ASA"]}
    ]
    app_client.delete(f"/api/collections/{cid}/members/998812623")
    assert app_client.get("/api/company/923609016").json()["collections"] == [cid]
    assert app_client.delete(f"/api/collections/{cid}").json() == {"ok": True}
    assert app_client.get(f"/api/collections/{cid}").status_code == 404


@pytest.mark.parametrize("name", ["", "x" * 81], ids=["empty", "too-long"])
def test_collection_name_bounds(app_client, name):
    assert app_client.post("/api/collections", json={"name": name}).status_code == 422


def test_other_users_collection_is_404(app_client, companies):
    from src.main import app

    cid = app_client.post("/api/collections", json={"name": "Min"}).json()["id"]
    with TestClient(
        app, headers={"Cookie": f"session={session_cookie('annen@example.com')}"}
    ) as other:
        assert [
            other.get(f"/api/collections/{cid}").status_code,
            other.patch(f"/api/collections/{cid}", json={"name": "x"}).status_code,
            other.put(f"/api/collections/{cid}/members/998812623").status_code,
            other.delete(f"/api/collections/{cid}/members/998812623").status_code,
            other.delete(f"/api/collections/{cid}").status_code,
        ] == [404] * 5
        assert other.get("/api/collections").json() == {"items": []}


def test_company_view_records_recent_and_favourite_flag(app_client, companies):
    app_client.get("/api/company/923609016")
    app_client.put("/api/favourites/998812623")
    body = app_client.get("/api/company/998812623").json()
    assert (body["favourite"], app_client.get("/api/recent").json()["items"]) == (
        True,
        [
            {"orgnr": "998812623", "name": "HJR HOLDING AS"},
            {"orgnr": "923609016", "name": "EQUINOR ASA"},
        ],
    )


def test_remove_from_recent(app_client, companies):
    app_client.get("/api/company/923609016")
    app_client.get("/api/company/998812623")
    assert app_client.delete("/api/recent/923609016").json() == {"ok": True}
    assert app_client.get("/api/recent").json()["items"] == [
        {"orgnr": "998812623", "name": "HJR HOLDING AS"}
    ]
    assert app_client.delete("/api/recent/923609016").status_code == 200  # already gone is fine
    assert app_client.delete("/api/recent/123").status_code == 400

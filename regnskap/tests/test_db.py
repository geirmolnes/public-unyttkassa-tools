"""SQLite storage: per-user isolation, recent cap, ownership replacement and owned_by."""

import pytest

from src import db

A, B = "a@example.com", "b@example.com"


def test_favourites_are_per_user_and_idempotent():
    db.add_favourite(A, "998812623", "HJR HOLDING AS")
    db.add_favourite(A, "998812623", "HJR HOLDING AS (nytt navn)")
    assert (db.list_favourites(A), db.list_favourites(B), db.is_favourite(B, "998812623")) == (
        [{"orgnr": "998812623", "name": "HJR HOLDING AS (nytt navn)"}],
        [],
        False,
    )
    db.remove_favourite(A, "998812623")
    assert db.list_favourites(A) == []


@pytest.mark.parametrize("views,kept", [(19, 19), (20, 20), (21, 20)], ids=["under", "at", "over"])
def test_recent_keeps_newest_twenty(views, kept):
    for i in range(views):
        db.touch_recent(A, f"{i:09d}", f"S{i}")
    items = db.list_recent(A, limit=100)
    assert (len(items), items[0]["orgnr"]) == (kept, f"{views - 1:09d}")


def test_recent_revisit_moves_to_top():
    db.touch_recent(A, "000000001", "En")
    db.touch_recent(A, "000000002", "To")
    db.touch_recent(A, "000000001", "En")
    assert [r["orgnr"] for r in db.list_recent(A)] == ["000000001", "000000002"]


def test_collections_belong_to_owner_and_cascade():
    cid = db.create_collection(A, "Børsnoterte", [("923609016", "EQUINOR ASA")])
    db.add_member(cid, "914778271", "Norsk Hydro ASA")
    assert db.get_collection(B, cid) is None
    assert (db.rename_collection(B, cid, "Kapret"), db.delete_collection(B, cid)) == (False, False)
    assert db.list_collections(A) == [
        {
            "id": cid,
            "name": "Børsnoterte",
            "count": 2,
            "preview": ["EQUINOR ASA", "Norsk Hydro ASA"],
        }
    ]
    assert db.collections_containing(A, "914778271") == [cid]
    assert db.delete_collection(A, cid)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM collection_members").fetchone()[0] == 0


def _row(name: str, orgnr: str | None = None) -> dict:
    return {
        "name": name,
        "orgnr": orgnr,
        "country": "Norge",
        "place": "Jessheim",
        "share_pct": 100.0,
        "page": 36,
    }


def test_replace_ownership_swaps_one_year_and_dedupes_names():
    db.replace_ownership(
        "998812623", 2025, [_row("Dibber AS", "998831067"), _row("Dibber AS")], "m"
    )
    db.replace_ownership("998812623", 2025, [_row("GTV 7 AS")], "m")
    assert [r["name"] for r in db.subsidiaries_of("998812623", 2025)] == ["GTV 7 AS"]


def test_owned_by_uses_parents_newest_year_only():
    db.replace_ownership("998812623", 2024, [_row("Dibber AS", "998831067")], "m")
    db.replace_ownership("998812623", 2025, [_row("GTV 7 AS")], "m")
    assert db.owned_by("998831067") == []
    db.replace_ownership("998812623", 2025, [_row("Dibber AS", "998831067")], "m")
    assert db.owned_by("998831067") == [
        {"orgnr": "998812623", "share_pct": 100.0, "source_year": 2025, "source_page": 36}
    ]


def test_init_db_fails_extractions_left_running():
    db.start_extraction("998812623", 2025, "m")
    db.init_db()
    ex = db.get_extraction("998812623", 2025)
    assert (ex["status"], ex["error"]) == ("failed", "Avbrutt av omstart. Prøv igjen.")

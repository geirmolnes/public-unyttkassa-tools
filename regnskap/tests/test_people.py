"""Person search: people in the shareholder register found by any word of the name."""

import pytest

from src import people
from tests.test_register import _build, line

A, B, C = "910000001", "910000002", "910000003"
YEARS = {
    2020: [
        line(A, "A AS", "TOR GAMMEL", "1940", 10, 100),
        line(A, "A AS", "OLA JACOB NORDBY", "1960", 10, 100),
    ],
    2021: [
        line(A, "A AS", "OLA JACOB NORDBY", "1960", 10, 100, place="0150 OSLO"),
        line(B, "B AS", "EVA NORDBYE", "1971", 5, 100),
        line(B, "B AS", "BJØRN ÅSE", "1980", 1, 100),
        line(B, "B AS", "NORDBY HOLDING AS", A, 50, 100),
        line(C, "C AS", "EVA NORDBYE", "1971", 5, 100),
        line(C, "C AS", "EVA NORDBYE", "1971", 1, 10, cls="B-aksjer"),
    ],
}


@pytest.fixture
def index(tmp_path, monkeypatch):
    def make(years=YEARS):
        monkeypatch.setenv("SHAREHOLDERS_DB_PATH", str(_build(tmp_path, years)))
        people.ensure_fresh(wait=True)

    make()
    return make


def _found(q: str) -> list[tuple]:
    return [
        (p["name"], p["birth_year"], p["companies"], p["current"])
        for p in people.search(q)["people"]
    ]


def test_any_word_prefix_best_first(index):
    """Still holding shares first, then most companies (share classes count once)."""
    assert _found("nordby") == [
        ("EVA NORDBYE", 1971, 2, True),
        ("OLA JACOB NORDBY", 1960, 1, True),
    ]
    assert _found("jac nord") == [("OLA JACOB NORDBY", 1960, 1, True)]
    assert _found("Ola  Jacob   NORDBY") == [("OLA JACOB NORDBY", 1960, 1, True)]


def test_former_holders_last_with_newest_address(index):
    assert _found("gammel") == [("TOR GAMMEL", 1940, 0, False)]
    ola = people.search("ola")["people"][0]
    assert (ola["postcode"], ola["place"], ola["last_year"]) == ("0150", "OSLO", 2021)


def test_norwegian_letters_are_their_own(index):
    assert _found("åse") == [("BJØRN ÅSE", 1980, 1, True)]
    assert _found("ase") == []
    assert _found("bjø") == [("BJØRN ÅSE", 1980, 1, True)]


def test_companies_and_nameless_words_are_not_people(index):
    assert _found("holding") == []
    assert _found("«»") == []


def test_rebuilt_when_the_register_changes(index):
    assert _found("nyfødt") == []
    index({**YEARS, 2022: [line(A, "A AS", "NYFØDT PERSON", "2001", 1, 100)]})
    assert _found("nyfødt") == [("NYFØDT PERSON", 2001, 1, True)]
    assert _found("nordbye") == [("EVA NORDBYE", 1971, 0, False)]  # not in 2022


def test_search_api_adds_people_on_request(app_client, brreg, index):
    brreg.search_rows = [{"organisasjonsnummer": A, "navn": "NORDBY HOLDING AS"}]
    plain = app_client.get("/api/search", params={"q": "nordby"}).json()
    both = app_client.get("/api/search", params={"q": "nordby", "people": "true"}).json()
    assert ([c["name"] for c in plain["results"]], plain["people"]) == (["NORDBY HOLDING AS"], [])
    assert [p["name"] for p in both["people"]] == ["EVA NORDBYE", "OLA JACOB NORDBY"]
    assert (both["people_ready"], [c["name"] for c in both["results"]]) == (
        True,
        ["NORDBY HOLDING AS"],
    )


def test_search_api_without_register(app_client, brreg, tmp_path, monkeypatch):
    monkeypatch.setenv("SHAREHOLDERS_DB_PATH", str(tmp_path / "missing.db"))
    brreg.search_rows = [{"organisasjonsnummer": A, "navn": "NORDBY HOLDING AS"}]
    body = app_client.get("/api/search", params={"q": "nordby", "people": "true"}).json()
    assert (len(body["results"]), body["people"], body["people_ready"]) == (1, [], False)


def test_exact_name_first_even_with_fewer_companies(index):
    """«ola haug» puts OLA HAUG first, though people with more companies also match."""
    rows = [line(c, f"{c} AS", "OLA HAUGEN", "1950", 1, 100) for c in (A, B, C)]
    rows += [line(c, f"{c} AS", "OLA ARNE HAUG", "1960", 1, 100) for c in (A, B)]
    rows += [line(A, "A AS", "OLA HAUG", "1970", 1, 100)]
    index({2021: sorted(rows, key=lambda r: r.split(";")[0])})
    assert [p["name"] for p in people.search("Ola  Haug")["people"]] == [
        "OLA HAUG",
        "OLA HAUGEN",
        "OLA ARNE HAUG",
    ]
    assert [p["name"] for p in people.search("ola haug", limit=1)["people"]] == ["OLA HAUG"]


def test_rebuilt_when_the_layout_changes(index, monkeypatch):
    assert not people._stale()
    monkeypatch.setattr(people, "LAYOUT", people.LAYOUT + 1)
    assert people._stale()
    people.ensure_fresh(wait=True)
    assert not people._stale()


STIANS = {
    2021: [line(A, "A AS", "KARI HANSEN", str(1960 + i), 1, 100) for i in range(8)]
    + [line(A, "A AS", "KARI HANSEN BAKKE", "1990", 1, 100)]
}


def test_total_counts_every_match_and_stops_at_the_cap(index, monkeypatch):
    index(STIANS)
    found = people.search("kari hansen")
    assert (len(found["people"]), found["total"], found["capped"]) == (people.LIMIT, 9, False)
    monkeypatch.setattr(people, "COUNT_MAX", 5)
    assert people.search("kari hansen") | {"people": None} == {
        "people": None,
        "total": 5,
        "capped": True,
        "ready": True,
    }


def test_people_api_lists_everyone_exact_names_first(app_client, brreg, index):
    index(STIANS)
    body = app_client.get("/api/people", params={"q": "kari hansen"}).json()
    names = [p["name"] for p in body["people"]]
    assert (names, body["total"]) == (["KARI HANSEN"] * 8 + ["KARI HANSEN BAKKE"], 9)
    assert sorted(p["birth_year"] for p in body["people"][:8]) == list(range(1960, 1968))
    search = app_client.get("/api/search", params={"q": "kari hansen", "people": "true"}).json()
    assert (len(search["people"]), search["people_total"], search["people_capped"]) == (6, 9, False)
    assert app_client.get("/api/people", params={"q": "s"}).json()["people"] == []

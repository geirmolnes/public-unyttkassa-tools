"""Shareholder register in the company view: owners, chain, holdings, change tags and history."""

from pathlib import Path

import pytest

from src import register, shareholders

HEADER = (
    "Orgnr;Selskap;Aksjeklasse;Navn aksjonær;Fødselsår/orgnr;Postnr/sted;Landkode;"
    "Antall aksjer;Antall aksjer selskap"
)
ORD = "Ordinære aksjer"
KID = "993031526"  # Fosseskansen-like: people + own shares
MID = "826157062"
TOP = "998831067"


def line(
    orgnr: str,
    company: str,
    holder: str,
    hid: str,
    shares: int,
    total: int,
    place="7000 TRONDHEIM",
    cls=ORD,
) -> str:
    return f"{orgnr};{company};{cls};{holder};{hid};{place};NO;{shares};{total}"


# Real shape of Fosseskansen Barnehage AS 2020–2022: own shares 40 → 50, one owner leaves,
# one moves (address change only), and MID owns KID 10 % throughout.
YEARS = {
    2020: [
        line(KID, "KID AS", "KID AS", KID, 40, 100),
        line(KID, "KID AS", "KARI HOLM", "1981", 10, 100),
        line(KID, "KID AS", "MARTE VOLD", "1989", 10, 100),
        line(KID, "KID AS", "MID AS", MID, 40, 100),
        line(MID, "MID AS", "TOP AS", TOP, 100, 100),
    ],
    2021: [
        line(KID, "KID AS", "KID AS", KID, 50, 100),
        line(KID, "KID AS", "MARTE VOLD", "1989", 10, 100, place="7010 TRONDHEIM"),
        line(KID, "KID AS", "MID AS", MID, 40, 100),
        line(MID, "MID AS", "TOP AS", TOP, 100, 100),
    ],
}


def _build(tmp_path: Path, years: dict[int, list[str]]) -> Path:
    db = tmp_path / "register.db"
    conn = shareholders.connect(db)
    paths = []
    for y, rows in years.items():
        p = tmp_path / f"aksjeeiebok_{y}.csv"
        p.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
        paths.append(p)
    shareholders.import_files(conn, paths)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    return db


@pytest.fixture
def reg(tmp_path, monkeypatch):
    def make(years=YEARS):
        monkeypatch.setenv("SHAREHOLDERS_DB_PATH", str(_build(tmp_path, years)))

    make()
    return make


def _holders(body: dict) -> list[tuple]:
    return [(h["name"], h["pct"], h["tag"]) for h in body["holders"]]


@pytest.mark.parametrize(
    "year,expected",
    [
        (
            2020,
            [
                ("KID AS", 40.0, None),
                ("MID AS", 40.0, None),
                ("KARI HOLM", 10.0, None),
                ("MARTE VOLD", 10.0, None),
            ],
        ),
        (2021, [("KID AS", 50.0, "up"), ("MID AS", 40.0, None), ("MARTE VOLD", 10.0, None)]),
        (None, [("KID AS", 50.0, "up"), ("MID AS", 40.0, None), ("MARTE VOLD", 10.0, None)]),
        (1999, [("KID AS", 50.0, "up"), ("MID AS", 40.0, None), ("MARTE VOLD", 10.0, None)]),
    ],
    ids=["first-year-no-tags", "second-year", "default-latest", "unknown-year-latest"],
)
def test_holders_and_tags(app_client, reg, year, expected):
    url = f"/api/company/{KID}/shareholders" + (f"?year={year}" if year else "")
    assert _holders(app_client.get(url).json()) == expected


def test_gone_and_change_count(app_client, reg):
    """Siri moved (address only): not a change. Linda left, own shares 40 → 50."""
    body = app_client.get(f"/api/company/{KID}/shareholders").json()
    assert ([g["name"] for g in body["gone"]], body["n_changes"]) == (["KARI HOLM"], 2)


def test_new_and_down_tags(app_client, reg):
    reg(
        {
            2020: [
                line(KID, "KID AS", "A", "1970", 60, 100),
                line(KID, "KID AS", "B", "1971", 40, 100),
            ],
            2021: [
                line(KID, "KID AS", "A", "1970", 50, 100),
                line(KID, "KID AS", "B", "1971", 40, 100),
                line(KID, "KID AS", "C", "1972", 10, 100),
            ],
        }
    )
    body = app_client.get(f"/api/company/{KID}/shareholders").json()
    assert _holders(body) == [("A", 50.0, "down"), ("B", 40.0, None), ("C", 10.0, "new")]


def test_same_share_after_split_is_no_change(app_client, reg):
    """A 1:10 split changes share counts, not ownership."""
    reg(
        {
            2020: [
                line(KID, "KID AS", "A", "1970", 30, 100),
                line(KID, "KID AS", "B", "1971", 70, 100),
            ],
            2021: [
                line(KID, "KID AS", "A", "1970", 300, 1000),
                line(KID, "KID AS", "B", "1971", 700, 1000),
            ],
        }
    )
    body = app_client.get(f"/api/company/{KID}/shareholders").json()
    assert (_holders(body), body["n_changes"]) == ([("B", 70.0, None), ("A", 30.0, None)], 0)


def test_chain_skips_own_shares_and_climbs(app_client, reg):
    chain = app_client.get(f"/api/company/{KID}/shareholders").json()["chain"]
    assert [[(o["name"], o["pct"], o["of"]) for o in lvl] for lvl in chain] == [
        [("MID AS", 40.0, KID), ("MARTE VOLD", 10.0, KID)],
        [("TOP AS", 100.0, MID)],
    ]


@pytest.mark.parametrize(
    "shares,in_chain",
    [(499, False), (500, True), (501, True)],
    ids=["4.99%", "5%", "5.01%"],
)
def test_chain_threshold_is_five_percent_inclusive(app_client, reg, shares, in_chain):
    reg(
        {
            2025: [
                line(KID, "KID AS", "BIG", "1970", 10000 - shares, 10000),
                line(KID, "KID AS", "SMALL", "1971", shares, 10000),
            ]
        }
    )
    chain = app_client.get(f"/api/company/{KID}/shareholders").json()["chain"]
    assert ("SMALL" in [o.get("name") for o in chain[0]]) is in_chain


@pytest.mark.parametrize("n,more", [(10, None), (11, 1)], ids=["at-cap", "one-over"])
def test_chain_level_capped_at_ten(app_client, reg, n, more):
    reg({2025: [line(KID, "KID AS", f"P{i:02}", str(1950 + i), 1, n) for i in range(n)]})
    level = app_client.get(f"/api/company/{KID}/shareholders").json()["chain"][0]
    assert (
        len([o for o in level if "name" in o]),
        next((o["more"] for o in level if "more" in o), None),
    ) == (10, more)


def test_chain_merges_share_classes_per_holder(app_client, reg):
    """Klevenstern shape: one owner across A/B/C shares is one 94 % line, not three. The register
    repeats the company total on every class, and the 5 % threshold applies to the sum."""
    reg(
        {
            2025: [
                line(KID, "KID AS", "KRISTIAN", "1961", 10, 100, cls="A-aksjer"),
                line(KID, "KID AS", "KRISTIAN", "1961", 15, 100, cls="B-aksjer"),
                line(KID, "KID AS", "KRISTIAN", "1961", 69, 100, cls="C-aksjer"),
                line(KID, "KID AS", "SPLIT", "1970", 3, 100, cls="A-aksjer"),
                line(KID, "KID AS", "SPLIT", "1970", 3, 100, cls="B-aksjer"),
            ]
        }
    )
    chain = app_client.get(f"/api/company/{KID}/shareholders").json()["chain"]
    assert [(o["name"], o["pct"]) for o in chain[0]] == [("KRISTIAN", 94.0), ("SPLIT", 6.0)]


def test_chain_stops_on_cycle(app_client, reg):
    reg(
        {
            2025: [
                line(KID, "KID AS", "MID AS", MID, 100, 100),
                line(MID, "MID AS", "KID AS", KID, 100, 100),
            ]
        }
    )
    chain = app_client.get(f"/api/company/{KID}/shareholders").json()["chain"]
    assert [[o["name"] for o in lvl] for lvl in chain] == [["MID AS"], ["KID AS"]]


def test_owns_excludes_own_shares(app_client, reg):
    body = app_client.get(f"/api/company/{MID}/shareholders").json()
    assert (
        [(o["orgnr"], o["name"], o["pct"]) for o in body["owns"]],
        body["owns_total"],
        body["owns_count"],
    ) == ([(KID, "KID AS", 40.0)], 1, {"2020": 1, "2021": 1})
    kid = app_client.get(f"/api/company/{KID}/shareholders").json()
    assert (kid["owns"], kid["owns_count"]) == ([], {})


@pytest.mark.parametrize("cap,large", [(7, False), (6, True)], ids=["at-limit", "one-over"])
def test_large_companies_get_top_list_only(app_client, reg, monkeypatch, cap, large):
    """KID has 7 holding rows over both years (4 + 3)."""
    monkeypatch.setattr(register, "HISTORY_MAX_ROWS", cap)
    body = app_client.get(f"/api/company/{KID}/shareholders").json()
    hist = app_client.get(f"/api/company/{KID}/shareholders/history").json()
    assert (body["large"], body["n_changes"] is None, hist["large"]) == (large, large, large)


def test_unknown_company_is_not_found(app_client, reg):
    body = app_client.get("/api/company/923609016/shareholders").json()
    assert (body["found"], body["holders"], body["chain"]) == (False, [], [])


def test_register_missing_is_503(app_client, tmp_path, monkeypatch):
    monkeypatch.setenv("SHAREHOLDERS_DB_PATH", str(tmp_path / "missing.db"))
    resp = app_client.get(f"/api/company/{KID}/shareholders")
    assert (resp.status_code, resp.json()["detail"]) == (
        503,
        "Aksjonærregisteret er ikke tilgjengelig akkurat nå.",
    )


def test_history_matrix_and_changes(app_client, reg):
    body = app_client.get(f"/api/company/{KID}/shareholders/history").json()
    assert [
        (r["name"], {y: (c["pct"], c["state"]) for y, c in r["by_year"].items()})
        for r in body["rows"]
    ] == [
        ("KID AS", {"2020": (40.0, None), "2021": (50.0, "changed")}),
        ("MID AS", {"2020": (40.0, None), "2021": (40.0, None)}),
        ("MARTE VOLD", {"2020": (10.0, None), "2021": (10.0, None)}),
        ("KARI HOLM", {"2020": (10.0, None)}),
    ]
    assert [
        (c["year"], c["kind"], c["name"], c["from_pct"], c["to_pct"]) for c in body["changes"]
    ] == [
        (2021, "out", "KARI HOLM", 10.0, None),
        (2021, "changed", "KID AS", 40.0, 50.0),
    ]


def test_invalid_orgnr_is_400(app_client, reg):
    assert app_client.get("/api/company/123/shareholders").status_code == 400


def test_chat_context_includes_register(app_client, reg, brreg, ai):
    brreg.company(KID, "KID AS", [2021])
    msg = {"orgnr": KID, "messages": [{"role": "user", "content": "Hvem eier?"}]}
    app_client.post("/api/chat", json=msg)
    lines = [
        line
        for line in ai.systems[0].splitlines()
        if line.startswith(("Aksjonærregisteret", "Eierkjede", "- 2021"))
    ]
    assert lines == [
        "Aksjonærregisteret (Skatteetaten, 2020–2021), år 2021: 3 aksjonærer.",
        "Eierkjede 1. ledd opp: MID AS (org.nr. 826157062) eier 40 % av KID AS; "
        "MARTE VOLD (født 1989) eier 10 % av KID AS",
        "Eierkjede 2. ledd opp: TOP AS (org.nr. 998831067) eier 100 % av MID AS",
        "- 2021: KARI HOLM (født 1981) gikk ut (hadde 10 %)",
        "- 2021: KID AS (egne aksjer) 40 % → 50 %",
    ]


def test_chat_context_without_register(app_client, brreg, ai, tmp_path, monkeypatch):
    monkeypatch.setenv("SHAREHOLDERS_DB_PATH", str(tmp_path / "missing.db"))
    brreg.company(KID, "KID AS", [2021])
    msg = {"orgnr": KID, "messages": [{"role": "user", "content": "Hvem eier?"}]}
    resp = app_client.post("/api/chat", json=msg)
    assert (resp.status_code, "Aksjonærregisteret" in ai.systems[0]) == (200, False)


def test_person_holdings_tags_and_changes(app_client, reg):
    reg(
        {
            2020: [
                line(KID, "KID AS", "MARTE VOLD", "1989", 10, 100),
                line(MID, "MID AS", "MARTE VOLD", "1989", 1, 3),
            ],
            2021: [
                line(KID, "KID AS", "MARTE VOLD", "1989", 20, 100, place="7010 TRONDHEIM"),
                line(TOP, "TOP AS", "MARTE VOLD", "1989", 5, 10),
            ],
        }
    )
    body = app_client.get("/api/person", params={"name": "MARTE VOLD", "born": 1989}).json()
    assert (
        [(h["orgnr"], h["name"], h["pct"], h["tag"]) for h in body["holdings"]],
        [g["name"] for g in body["gone"]],
        body["address"],
        body["counts"],
        [(c["year"], c["kind"], c["name"]) for c in body["changes"]],
    ) == (
        [(TOP, "TOP AS", 50.0, "new"), (KID, "KID AS", 20.0, "up")],
        ["MID AS"],
        {"year": 2021, "postcode": "7010", "place": "TRONDHEIM", "country": "NO"},
        {"2020": 2, "2021": 2},
        [(2021, "out", "MID AS"), (2021, "in", "TOP AS"), (2021, "changed", "KID AS")],
    )
    kid = next(c for c in body["changes"] if c["name"] == "KID AS")
    assert (body["changes"][0]["from_pct"], kid["from_shares"], kid["to_shares"]) == (
        33.3333,
        10,
        20,
    )


@pytest.mark.parametrize(
    "params,found",
    [
        ({"name": "MARTE VOLD", "born": 1989}, True),
        ({"name": "MARTE VOLD", "born": 1990}, False),
        ({"name": "MARTE VOLD"}, False),
        ({"name": "  MARTE VOLD  ", "born": 1989}, True),
    ],
    ids=["match", "other-birth-year", "no-birth-year", "padded-name"],
)
def test_person_is_name_plus_birth_year(app_client, reg, params, found):
    assert app_client.get("/api/person", params=params).json()["found"] is found


@pytest.mark.parametrize(
    "params,status",
    [
        ({"name": ""}, 422),
        ({"name": "X" * 201}, 422),
        ({"name": "X" * 200}, 200),
        ({"name": "A", "born": 1799}, 422),
        ({"name": "A", "born": 1800}, 200),
        ({"name": "A", "born": 2100}, 200),
        ({"name": "A", "born": 2101}, 422),
    ],
    ids=["empty", "201-chars", "200-chars", "born-1799", "born-1800", "born-2100", "born-2101"],
)
def test_person_param_bounds(app_client, reg, params, status):
    assert app_client.get("/api/person", params=params).status_code == status


def test_person_foreign_without_birth_year(app_client, reg):
    reg({2025: [f"{KID};KID AS;{ORD};QWELL HOLDING LTD.; ;, Dubai;AE;5;10"]})
    body = app_client.get("/api/person", params={"name": "QWELL HOLDING LTD."}).json()
    assert [(h["name"], h["pct"]) for h in body["holdings"]] == [("KID AS", 50.0)]

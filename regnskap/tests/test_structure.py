"""Final owners (look-through), the group summary and the saved eierkart."""

import pytest

from src import register
from tests.test_register import ORD, _build, line

ROOT = "993031526"
H1, H2, I1, I2 = "900000001", "900000002", "900000011", "900000012"
S1, S2, S11, S12, S21, S22, JV, X = (
    "900000101", "900000102", "900000111", "900000112", "900000121", "900000122", "900000199",
    "900000103",
)  # fmt: skip

# NorgesGruppen shape: a family holding company reached directly and through a second company,
# owned by two invest companies, each owned by one person. Below, two subsidiaries share a joint
# venture, and one holds a company at exactly 20 % and another just under.
GROUP = {
    2025: [
        line(ROOT, "ROOT ASA", "H1 HANDEL AS", H1, 64, 100),
        line(ROOT, "ROOT ASA", "H2 HANDEL II AS", H2, 10, 100),
        line(ROOT, "ROOT ASA", "PER NULL", "1950", 26, 100),
        line(H2, "H2 HANDEL II AS", "H1 HANDEL AS", H1, 100, 100),
        line(H1, "H1 HANDEL AS", "I1 INVEST AS", I1, 50, 100),
        line(H1, "H1 HANDEL AS", "I2 INVEST AS", I2, 50, 100),
        line(I1, "I1 INVEST AS", "ANNE A", "1990", 100, 100),
        line(I2, "I2 INVEST AS", "BJØRN B", "1992", 100, 100),
        line(S1, "S1 AS", "ROOT ASA", ROOT, 100, 100),
        line(S2, "S2 AS", "ROOT ASA", ROOT, 100, 100),
        line(X, "X AS", "ROOT ASA", ROOT, 5, 100),
        line(S11, "S11 AS", "S1 AS", S1, 100, 100),
        line(S12, "S12 AS", "S1 AS", S1, 100, 100),
        line(JV, "JV AS", "S1 AS", S1, 50, 100),
        line(JV, "JV AS", "S2 AS", S2, 50, 100),
        line(S21, "S21 AS", "S2 AS", S2, 1999, 10000),
        line(S22, "S22 AS", "S2 AS", S2, 2000, 10000),
    ]
}


@pytest.fixture
def group(tmp_path, monkeypatch):
    def make(years=GROUP):
        monkeypatch.setenv("SHAREHOLDERS_DB_PATH", str(_build(tmp_path, years)))

    make()
    return make


def _ult(body: dict) -> list[tuple]:
    return [(o["name"], o["pct"]) for o in body["ultimate"]["owners"]]


def test_final_owners_sum_every_path(app_client, group):
    """ANNE owns 50 % of H1, which holds 64 % directly and 10 % through H2: 37 %."""
    body = app_client.get(f"/api/company/{ROOT}/shareholders").json()
    anne = body["ultimate"]["owners"][0]
    assert (_ult(body), body["ultimate"]["traced"], [t["name"] for t in anne["through"]]) == (
        [("ANNE A", 37.0), ("BJØRN B", 37.0), ("PER NULL", 26.0)],
        100.0,
        ["I1 INVEST AS", "H1 HANDEL AS"],
    )


@pytest.mark.parametrize("shares,listed", [(99, False), (100, True)], ids=["0.99%", "1%"])
def test_final_owners_under_one_percent_count_as_spread(app_client, group, shares, listed):
    """H holds 10 %; X holds shares/1000 of H, so X carries 0.99 % or 1 % of ROOT."""
    group(
        {
            2025: [
                line(ROOT, "ROOT ASA", "BIG", "1950", 90, 100),
                line(ROOT, "ROOT ASA", "H AS", H1, 10, 100),
                line(H1, "H AS", "X", "1960", shares, 1000),
                line(H1, "H AS", "Y", "1961", 1000 - shares, 1000),
            ]
        }
    )
    ult = app_client.get(f"/api/company/{ROOT}/shareholders").json()["ultimate"]
    assert (("X" in [o["name"] for o in ult["owners"]]), ult["traced"]) == (
        listed,
        100.0 if listed else 99.01,
    )


def test_final_owner_reached_two_ways_gets_both(app_client, group):
    """JOHAN holds 3 % of H1, which holds 64 % directly and 10 % through H2. Through H2 alone he
    carries 0.3 %, but it adds up with the rest: 3 % of 74 %."""
    group(
        {
            2025: [
                line(ROOT, "ROOT ASA", "H1 AS", H1, 64, 100),
                line(ROOT, "ROOT ASA", "H2 AS", H2, 10, 100),
                line(ROOT, "ROOT ASA", "REST", "1950", 26, 100),
                line(H2, "H2 AS", "H1 AS", H1, 100, 100),
                line(H1, "H1 AS", "JOHAN", "1967", 3, 100),
                line(H1, "H1 AS", "ANNE", "1990", 97, 100),
            ]
        }
    )
    owners = app_client.get(f"/api/company/{ROOT}/shareholders").json()["ultimate"]["owners"]
    assert [(o["name"], o["pct"]) for o in owners] == [
        ("ANNE", 71.78),
        ("REST", 26.0),
        ("JOHAN", 2.22),
    ]


def test_final_owners_stop_at_companies_without_owners(app_client, group):
    """A ministry has no owners in the register and a foreign bank's ID isn't Norwegian: both
    are where the chain ends."""
    group(
        {
            2025: [
                line(ROOT, "ROOT ASA", "DEPARTEMENTET", "912660680", 67, 100),
                f"{ROOT};ROOT ASA;{ORD};STATE STREET BANK;200326474;, BOSTON;US;33;100",
            ]
        }
    )
    owners = app_client.get(f"/api/company/{ROOT}/shareholders").json()["ultimate"]["owners"]
    assert [(o["name"], o["orgnr"], o["pct"], o["through"]) for o in owners] == [
        ("DEPARTEMENTET", "912660680", 67.0, []),
        ("STATE STREET BANK", "200326474", 33.0, []),
    ]


def test_final_owners_follow_a_loop_until_it_fades(app_client, group):
    """C1 → C2 → C1: what C1 holds of C2 comes round again, halving each time, so R, the only
    owner outside the loop, ends up with C1's whole half."""
    c1, c2 = H1, H2
    group(
        {
            2025: [
                line(ROOT, "ROOT ASA", "C1 AS", c1, 50, 100),
                line(ROOT, "ROOT ASA", "Q", "1970", 50, 100),
                line(c1, "C1 AS", "C2 AS", c2, 100, 100),
                line(c2, "C2 AS", "C1 AS", c1, 50, 100),
                line(c2, "C2 AS", "R", "1971", 50, 100),
            ]
        }
    )
    body = app_client.get(f"/api/company/{ROOT}/shareholders").json()
    assert [(n, round(p, 2)) for n, p in _ult(body)] == [("Q", 50.0), ("R", 49.99)]


def test_final_owners_drop_stake_that_comes_back_to_the_company(app_client, group):
    """S holds 10 % of ROOT and is wholly owned by ROOT: like own shares, that 10 % has no owner."""
    group(
        {
            2025: [
                line(ROOT, "ROOT ASA", "S AS", S1, 10, 100),
                line(ROOT, "ROOT ASA", "P", "1970", 90, 100),
                line(S1, "S AS", "ROOT ASA", ROOT, 100, 100),
            ]
        }
    )
    body = app_client.get(f"/api/company/{ROOT}/shareholders").json()
    assert (_ult(body), body["ultimate"]["traced"]) == ([("P", 90.0)], 90.0)


def test_group_summary_on_company_page(app_client, group):
    group_ = app_client.get(f"/api/company/{ROOT}/shareholders").json()["group"]
    assert (
        {k: group_[k] for k in ("direct", "companies", "depth", "rest", "rest_under")},
        [(b["name"], b["pct"], b["kind"], b["under"]) for b in group_["branches"]],
    ) == (
        {"direct": 3, "companies": 6, "depth": 2, "rest": 0, "rest_under": 0},
        [("S1 AS", 100.0, "own", 3), ("S2 AS", 100.0, "own", 2), ("X AS", 5.0, "small", None)],
    )


def test_group_summary_folds_the_rest(app_client, group, monkeypatch):
    monkeypatch.setattr(register, "MAP_BRANCHES", 1)
    group_ = app_client.get(f"/api/company/{ROOT}/shareholders").json()["group"]
    assert ([b["name"] for b in group_["branches"]], group_["rest"], group_["rest_under"]) == (
        ["S1 AS"],
        2,
        2,
    )


def test_company_without_holdings_has_no_group(app_client, group):
    assert app_client.get(f"/api/company/{ROOT}/shareholders").json()["group"] is not None
    assert app_client.get(f"/api/company/{S11}/shareholders").json()["group"] is None


def test_structure_down_lists_a_shared_company_under_each_parent(app_client, group):
    """JV is held 50/50 by S1 and S2: both list it as opened, and both count it under them."""
    body = app_client.post(f"/api/company/{ROOT}/structure").json()
    assert (body["down"], body["under"], body["stats"]) == (
        {
            ROOT: [[S1, 100.0, "own"], [S2, 100.0, "own"], [X, 5.0, "small"]],
            S1: [[S11, 100.0, "own"], [S12, 100.0, "own"], [JV, 50.0, "own"]],
            S2: [[JV, 50.0, "own"], [S22, 20.0, "own"], [S21, 19.99, "small"]],
        },
        {ROOT: 6, S1: 3, S2: 2, S11: 0, S12: 0, JV: 0, S22: 0},
        {
            "companies": 6,
            "down_depth": 2,
            "small": 2,
            "truncated": False,
            "owners": 7,
            "up_depth": 3,
        },
    )


def test_structure_up_lists_a_shared_owner_under_each_and_climbs_it_once(app_client, group):
    """H1 owns ROOT directly and through H2: H2 lists it like any owner, H1's owners once."""
    body = app_client.post(f"/api/company/{ROOT}/structure").json()
    assert body["up"] == {
        ROOT: [[H1, 64.0, "own"], ["PER NULL|1950", 26.0, "own"], [H2, 10.0, "own"]],
        H1: [[I1, 50.0, "own"], [I2, 50.0, "own"]],
        H2: [[H1, 100.0, "own"]],
        I1: [["ANNE A|1990", 100.0, "own"]],
        I2: [["BJØRN B|1992", 100.0, "own"]],
    }
    assert body["nodes"]["ANNE A|1990"] == {
        "name": "ANNE A",
        "orgnr": None,
        "birth_year": 1990,
        "country": "NO",
    }


@pytest.mark.parametrize(
    "stake,down,under",
    [
        (19, {ROOT: [[S1, 100.0, "own"]], S1: [[ROOT, 19.0, "small"]]}, {ROOT: 1, S1: 0}),
        (20, {ROOT: [[S1, 100.0, "own"]], S1: [[ROOT, 20.0, "own"]]}, {ROOT: 1, S1: 1}),
    ],
    ids=["19%-not-followed", "20%-loops-back"],
)
def test_structure_down_loop_back_to_the_company_is_listed_once(
    app_client, group, stake, down, under
):
    """S1 is wholly owned by ROOT and holds `stake` % of ROOT back: the map stops the loop."""
    group(
        {
            2025: [
                line(S1, "S1 AS", "ROOT ASA", ROOT, 100, 100),
                line(ROOT, "ROOT ASA", "S1 AS", S1, stake, 100),
                line(ROOT, "ROOT ASA", "P", "1970", 100 - stake, 100),
            ]
        }
    )
    body = app_client.post(f"/api/company/{ROOT}/structure").json()
    assert (body["down"], body["under"], body["stats"]["companies"]) == (down, under, 1)


def test_structure_up_loop_back_to_the_company_is_climbed_once(app_client, group):
    """C1 owns ROOT, C2 owns C1, and C1 owns C2: C2 lists C1 as an owner, which isn't climbed again."""
    c1, c2 = H1, H2
    group(
        {
            2025: [
                line(ROOT, "ROOT ASA", "C1 AS", c1, 100, 100),
                line(c1, "C1 AS", "C2 AS", c2, 100, 100),
                line(c2, "C2 AS", "C1 AS", c1, 60, 100),
                line(c2, "C2 AS", "Q", "1970", 40, 100),
            ]
        }
    )
    body = app_client.post(f"/api/company/{ROOT}/structure").json()
    assert (body["up"], body["stats"]["owners"]) == (
        {
            ROOT: [[c1, 100.0, "own"]],
            c1: [[c2, 100.0, "own"]],
            c2: [[c1, 60.0, "own"], ["Q|1970", 40.0, "own"]],
        },
        3,
    )


def test_structure_depth_cap_marks_truncated(app_client, group, monkeypatch):
    monkeypatch.setattr(register, "STRUCTURE_DEPTH", 1)
    body = app_client.post(f"/api/company/{ROOT}/structure").json()
    assert (S1 in body["down"], body["stats"]["truncated"]) == (True, True)
    assert [kind for _, _, kind in body["down"][S1]] == ["small", "small", "small"]


def test_structure_is_saved_and_reused(app_client, group):
    url = f"/api/company/{ROOT}/structure"
    before = app_client.get(url)
    made = app_client.post(url).json()
    saved = app_client.get(url).json()
    snap = app_client.get(f"/api/company/{ROOT}/shareholders").json()
    assert (before.status_code, before.json()["detail"]) == (
        404,
        "Selskapsstrukturen er ikke laget ennå.",
    )
    assert (saved, snap["structure_saved"]) == (made, made["created_at"])


def test_saved_structure_from_older_rules_is_ignored(app_client, group, monkeypatch):
    url = f"/api/company/{ROOT}/structure"
    app_client.post(url)
    monkeypatch.setattr(register, "STRUCTURE_VERSION", register.STRUCTURE_VERSION + 1)
    snap = app_client.get(f"/api/company/{ROOT}/shareholders").json()
    assert (app_client.get(url).status_code, snap["structure_saved"]) == (404, None)


def test_saved_structure_is_per_year(app_client, group):
    group({2024: GROUP[2025][:3], 2025: GROUP[2025]})
    url = f"/api/company/{ROOT}/structure"
    app_client.post(url, params={"year": 2024})
    assert (
        app_client.get(url, params={"year": 2024}).json()["year"],
        app_client.get(url).status_code,
    ) == (2024, 404)


def test_structure_of_unknown_company_is_404(app_client, group):
    resp = app_client.post("/api/company/923609016/structure")
    assert resp.status_code == 404


@pytest.mark.parametrize("method", ["get", "post"])
def test_structure_without_register_is_503(app_client, tmp_path, monkeypatch, method):
    monkeypatch.setenv("SHAREHOLDERS_DB_PATH", str(tmp_path / "missing.db"))
    resp = getattr(app_client, method)(f"/api/company/{ROOT}/structure")
    assert resp.status_code == 503


def test_chat_context_has_final_owners_and_group(app_client, group, brreg, ai):
    brreg.company(ROOT, "ROOT ASA", [2025])
    msg = {"orgnr": ROOT, "messages": [{"role": "user", "content": "Hvem eier egentlig?"}]}
    app_client.post("/api/chat", json=msg)
    lines = [
        ln for ln in ai.systems[0].splitlines() if ln.startswith(("Endelige eiere", "Eier i alle"))
    ]
    assert lines == [
        "Endelige eiere (andeler ganget gjennom alle ledd, eiere under 1 % utelatt): "
        "ANNE A (født 1990) 37 % gjennom I1 INVEST AS → H1 HANDEL AS; "
        "BJØRN B (født 1992) 37 % gjennom I2 INVEST AS → H1 HANDEL AS; "
        "PER NULL (født 1950) 26 %. Til sammen 100 % sporet.",
        "Eier i alle ledd (fulgt der andelen er minst 20 %): 6 selskaper i 2 ledd, 3 direkte. "
        "Største grener: S1 AS 100 % (3 selskaper under), S2 AS 100 % (2 selskaper under), "
        "X AS 5 %",
    ]


def test_rule_is_stated_where_its_numbers_are(app_client, group):
    """The page says which stakes were followed next to every count that depends on it."""
    snap = app_client.get(f"/api/company/{ROOT}/shareholders").json()
    made = app_client.post(f"/api/company/{ROOT}/structure").json()
    assert ((snap["follow_rule"], snap["follow_below"]), made["rules"]) == (
        ("minst 20 %", "under 20 %"),
        {"follow": "minst 20 %", "below": "under 20 %", "owner_pct": 5.0},
    )


# Under the 20 % line: ROOT holds 75 % of A; A holds B (just under or exactly 20 %), which holds
# C and D. KARI holds the rest of A and 10 % of B herself.
A, B, C, D = "900000200", "900000219", "900000227", "900000235"


def _line_group(b_shares: int) -> dict[int, list[str]]:
    return {
        2025: [
            line(A, "A AS", "ROOT ASA", ROOT, 75, 100),
            line(A, "A AS", "KARI K", "1970", 25, 100),
            line(B, "B AS", "A AS", A, b_shares, 10000),
            line(B, "B AS", "KARI K", "1970", 1000, 10000),
            line(C, "C AS", "B AS", B, 100, 100),
            line(D, "D AS", "B AS", B, 50, 100),
        ]
    }


@pytest.mark.parametrize(
    "b_shares,b_kind,more",
    [(1999, "small", {B: 2}), (2000, "own", {})],
    ids=["19.99%-small-counts-its-holdings", "20%-followed"],
)
def test_structure_counts_what_a_small_stake_holds(app_client, group, b_shares, b_kind, more):
    group(_line_group(b_shares))
    body = app_client.post(f"/api/company/{ROOT}/structure").json()
    assert ([e[2] for e in body["down"][A]], body["more"]) == ([b_kind], more)


def test_holdings_tree_follows_a_small_stake_on_request(app_client, group):
    group(_line_group(1999))
    body = app_client.get(f"/api/company/{B}/holdings-tree").json()
    assert body == {
        "edges": {B: [[C, 100.0, "own"], [D, 50.0, "own"]]},
        "names": {C: "C AS", D: "D AS"},
        "under": {B: 2, C: 0, D: 0},
        "more": {},
        "year": 2025,
    }


def test_person_structure_is_made_saved_and_reported(app_client, group):
    group(_line_group(1999))
    q = "/api/person/structure?name=KARI%20K&born=1970"
    before = app_client.get(q)
    made = app_client.post(q).json()
    person = app_client.get("/api/person?name=KARI%20K&born=1970").json()
    assert (before.status_code, before.json()["detail"]) == (
        404,
        "Eierstrukturen er ikke laget ennå.",
    )
    assert (made["kind"], made["orgnr"], made["name"], made["down"]["KARI K|1970"]) == (
        "person",
        "KARI K|1970",
        "KARI K",
        [[A, 25.0, "own"], [B, 10.0, "small"]],
    )
    assert (made["more"], app_client.get(q).json(), person["structure_saved"]) == (
        {B: 2},
        made,
        made["created_at"],
    )
    assert (person["group"]["direct"], person["follow_rule"]) == (2, "minst 20 %")


def test_person_structure_of_someone_without_shares_is_404(app_client, group):
    resp = app_client.post("/api/person/structure?name=NOEN%20ANDRE&born=1970")
    assert (resp.status_code, resp.json()["detail"]) == (
        404,
        "Personen har ingen aksjer i aksjonærregisteret.",
    )

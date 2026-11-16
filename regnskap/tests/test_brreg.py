"""Brreg client: parsing, lookup fallbacks and retry behaviour (Brreg mocked)."""

import json
from pathlib import Path

import httpx
import pytest

from src.brreg import (
    BrregClient,
    BrregUnavailable,
    NotAvailable,
    parse_key_figures,
    parse_key_figures_history,
    parse_roles,
)
from tests.conftest import BANK_500

FIXTURES = Path(__file__).parent / "fixtures"


async def test_search_by_orgnr_uses_direct_lookup_and_finds_deleted(brreg, client_brreg):
    brreg.enheter["934222695"] = {
        "organisasjonsnummer": "934222695",
        "navn": "VSALES AS",
        "respons_klasse": "SlettetEnhet",
        "slettedato": "2025-06-02",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
    }
    [company] = await client_brreg.search("934 222 695")
    assert (company.name, company.kind, company.deleted_date) == (
        "VSALES AS",
        "slettet",
        "2025-06-02",
    )


async def test_search_by_unknown_orgnr_returns_empty(brreg, client_brreg):
    assert await client_brreg.search("998812623") == []


async def test_search_by_name_empty_result(brreg, client_brreg):
    assert await client_brreg.search("finnes ikke") == []


async def test_get_company_falls_back_to_underenhet(brreg, client_brreg):
    brreg.underenhet("933158381", "HJR AS", parent="833155482")
    company = await client_brreg.get_company("933158381")
    assert (company.kind, company.parent_orgnr) == ("underenhet", "833155482")


async def test_list_years_unknown_orgnr_is_empty(brreg, client_brreg):
    assert await client_brreg.list_years("998812623") == []


async def test_fetch_pdf_missing_year_raises_not_available(brreg, client_brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2024])
    with pytest.raises(NotAvailable, match="1999"):
        await client_brreg.fetch_pdf("998812623", 1999)


@pytest.mark.parametrize(
    "failures,ok", [(1, True), (2, True), (3, False)], ids=["1-retry", "2-retries", "gives-up"]
)
async def test_fetch_pdf_retries_429(brreg, client_brreg, failures, ok):
    brreg.company("998812623", "HJR HOLDING AS", [2024])
    brreg.queued = [httpx.Response(429) for _ in range(failures)]
    if ok:
        assert (await client_brreg.fetch_pdf("998812623", 2024)).startswith(b"%PDF")
    else:
        with pytest.raises(BrregUnavailable, match="429"):
            await client_brreg.fetch_pdf("998812623", 2024)


@pytest.mark.parametrize(
    "headers,wait",
    [
        ({"x-rate-limit-retry-after-seconds": "15"}, 15.0),
        ({"x-rate-limit-retry-after-seconds": "29"}, 29.0),
        ({"x-rate-limit-retry-after-seconds": "30"}, 30.0),
        ({"x-rate-limit-retry-after-seconds": "31"}, 30.0),
        ({"x-rate-limit-retry-after-seconds": "1"}, 1.0),
        ({"x-rate-limit-retry-after-seconds": "0"}, 2.0),
        ({"x-rate-limit-retry-after-seconds": "soon"}, 2.0),
        ({"retry-after": "5", "x-rate-limit-retry-after-seconds": "15"}, 5.0),
        ({}, 2.0),
    ],
    ids=["brreg-15", "brreg-29", "brreg-cap", "brreg-over-cap", "brreg-1", "brreg-0", "brreg-junk",
         "retry-after-first", "no-header"],
)  # fmt: skip
async def test_429_waits_as_long_as_brreg_says(brreg, monkeypatch, headers, wait):
    """Regression: Brreg's accounts register says how long in x-rate-limit-retry-after-seconds; we
    only read Retry-After, so retried after 1 and 2 s inside the same window and gave up."""
    waits: list[float] = []

    async def fake_sleep(s: float) -> None:
        waits.append(s)

    monkeypatch.setattr("src.brreg.asyncio.sleep", fake_sleep)
    brreg.company("998812623", "HJR HOLDING AS", [2024])
    brreg.queued = [httpx.Response(429, headers=headers)]
    async with httpx.AsyncClient() as http:
        years = await BrregClient(http, backoff=2.0).list_years("998812623")
    assert (years, waits) == ([2024], [wait])


@pytest.mark.parametrize(
    "body,outcome,calls",
    [
        (BANK_500, [], 1),
        ({"status": "500", "message": "Noe gikk galt"}, "BrregUnavailable", 3),
    ],
    ids=["bank-layout", "other-500"],
)
async def test_figure_rows_takes_brregs_bank_500_as_no_figures(
    brreg, client_brreg, body, outcome, calls
):
    brreg.queued = [httpx.Response(500, json=body)] * 3
    try:
        got = await client_brreg.figure_rows("984851006")
    except BrregUnavailable as e:
        got = type(e).__name__
    assert (got, len(brreg.paths)) == (outcome, calls)


async def test_fetch_pdf_rejects_non_pdf_body(brreg, client_brreg):
    brreg.queued = [httpx.Response(200, content=b"<html>maintenance</html>")]
    with pytest.raises(BrregUnavailable, match="ikke er en PDF"):
        await client_brreg.fetch_pdf("998812623", 2024)


async def test_transport_error_retries_then_raises(client_brreg):
    import respx

    with respx.mock() as router:
        route = router.get(url__startswith="https://data.brreg.no").mock(
            side_effect=httpx.ConnectError("boom")
        )
        with pytest.raises(BrregUnavailable, match="ConnectError"):
            await client_brreg.list_years("998812623")
    assert route.call_count == 3


def test_parse_key_figures_real_hjr_json():
    """Regression: full statement from Brreg's real response shape (HJR HOLDING AS 2025)."""
    rows = json.loads((FIXTURES / "hjr_regnskap_2025.json").read_text())
    kf = parse_key_figures([{"regnskapstype": "KONSERN"}, *rows])
    assert {k: v for k, v in kf.items() if k != "ek_andel"} == {
        "year": 2025,
        "from_date": "2025-01-01",
        "to_date": "2025-12-31",
        "currency": "NOK",
        "driftsinntekter": 257000.0,
        "salgsinntekter": None,
        "driftskostnader": 1311000.0,
        "loennskostnad": None,
        "driftsresultat": -1054000.0,
        "finansinntekter": 33891000.0,
        "finanskostnader": 126000.0,
        "rentekostnad_konsern": None,
        "rentekostnader": None,
        "netto_finans": 33765000.0,
        "resultat_for_skatt": 32711000.0,
        "skattekostnad": None,
        "ekstraordinaere_poster": None,
        "skatt_ekstraordinaert": None,
        "skattekostnad_avledet": 7447000.0,
        "aarsresultat": 25264000.0,
        "totalresultat": None,
        "anleggsmidler": 78781000.0,
        "goodwill": None,
        "omlopsmidler": 35309000.0,
        "varer": None,
        "fordringer": None,
        "investeringer": None,
        "bankinnskudd": None,
        "sum_eiendeler": 114090000.0,
        "innskutt_egenkapital": 1270000.0,
        "opptjent_egenkapital": 28699000.0,
        "egenkapital": 29969000.0,
        "langsiktig_gjeld": 74649000.0,
        "kortsiktig_gjeld": 9472000.0,
        "sum_gjeld": 84121000.0,
        "revidert": True,
        "smaa_foretak": False,
        "regnskapsregler": "regnskapslovenAlminneligRegler",
        "morselskap": True,
        "avviklingsregnskap": False,
        "archived": False,
    }
    assert kf["ek_andel"] == pytest.approx(0.2627, abs=1e-4)


def test_parse_key_figures_picks_newest_of_several_years():
    """Regression: Brreg sends 2023–2025 oldest first (plus KONSERN rows); the first SELSKAP row was shown as "latest"."""
    rows = json.loads((FIXTURES / "hjr_regnskap_2023_2025.json").read_text())
    assert parse_key_figures(rows)["year"] == 2025
    assert parse_key_figures(list(reversed(rows)))["year"] == 2025


def test_parse_key_figures_history_newest_first_selskap_only():
    rows = json.loads((FIXTURES / "hjr_regnskap_2023_2025.json").read_text())
    history = parse_key_figures_history(rows)
    assert [(kf["year"], kf["aarsresultat"]) for kf in history] == [
        (2025, 25264000.0),
        (2024, 25783920.0),
        (2023, 4626865.0),
    ]


def test_parse_key_figures_extra_fields():
    """Lines Brreg added beyond the sums: real tax, salaries, cash."""
    rows = json.loads((FIXTURES / "hjr_regnskap_2023_2025.json").read_text())
    kf = parse_key_figures(rows)
    assert (kf["skattekostnad"], kf["loennskostnad"], kf["bankinnskudd"]) == (
        7447000.0,
        57000.0,
        1660000.0,
    )


def test_parse_key_figures_history_konsern():
    """Group accounts are a second set of rows; HJR's group lost money while the company earned."""
    rows = json.loads((FIXTURES / "hjr_regnskap_2023_2025.json").read_text())
    history = parse_key_figures_history(rows, "KONSERN")
    assert [(kf["year"], kf["aarsresultat"], kf["goodwill"]) for kf in history] == [
        (2025, -116892000.0, 1547036000.0),
        (2024, -199054000.0, 1528955000.0),
        (2023, -74781000.0, 1380206000.0),
    ]


@pytest.mark.parametrize(
    "res,eiendeler,key,expected",
    [
        ({"totalresultat": -144930000.0}, {}, "totalresultat", -144930000.0),
        ({"ekstraordinaerePoster": 5.0}, {}, "ekstraordinaere_poster", 5.0),
        ({"skattekostnadEkstraordinaertResultat": 1.0}, {}, "skatt_ekstraordinaert", 1.0),
        (
            {"finansresultat": {"finanskostnad": {"rentekostnadSammeKonsern": 30985.0}}},
            {},
            "rentekostnad_konsern",
            30985.0,
        ),
        ({}, {"sumInvesteringer": 7.0}, "investeringer", 7.0),
        ({}, {"goodwill": 0.0}, "goodwill", 0.0),
    ],
    ids=[
        "totalresultat",
        "ekstraordinaer",
        "skatt-ekstra",
        "rente-konsern",
        "investeringer",
        "goodwill-zero",
    ],
)
def test_parse_key_figures_optional_lines(res, eiendeler, key, expected):
    row = {"regnskapstype": "SELSKAP", "resultatregnskapResultat": res, "eiendeler": eiendeler}
    assert parse_key_figures([row])[key] == expected


@pytest.mark.parametrize(
    "flag,expected", [(True, True), (False, False), (None, False)], ids=["true", "false", "missing"]
)
def test_parse_key_figures_liquidation_flag(flag, expected):
    row = {"regnskapstype": "SELSKAP"}
    if flag is not None:
        row["avviklingsregnskap"] = flag
    assert parse_key_figures([row])["avviklingsregnskap"] is expected


def test_parse_key_figures_history_empty():
    assert parse_key_figures_history([{"regnskapstype": "KONSERN"}]) == []


@pytest.mark.parametrize(
    "revisjon,expected",
    [
        ({"ikkeRevidertAarsregnskap": False, "fravalgRevisjon": False}, True),
        ({"ikkeRevidertAarsregnskap": True, "fravalgRevisjon": False}, False),
        ({"ikkeRevidertAarsregnskap": False, "fravalgRevisjon": True}, False),
        ({}, None),
    ],
    ids=["revidert", "ikke-revidert", "fravalgt", "ukjent"],
)
def test_parse_key_figures_audit_flag(revisjon, expected):
    assert (
        parse_key_figures([{"regnskapstype": "SELSKAP", "revisjon": revisjon}])["revidert"]
        is expected
    )


@pytest.mark.parametrize(
    "egenkapital,eiendeler,expected",
    [(50.0, 100.0, 0.5), (-10.0, 100.0, -0.1), (50.0, 0.0, None), (None, 100.0, None)],
    ids=["normal", "negativ-ek", "null-eiendeler", "mangler-ek"],
)
def test_ek_andel_boundaries(egenkapital, eiendeler, expected):
    row = {
        "regnskapstype": "SELSKAP",
        "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": egenkapital}},
        "eiendeler": {"sumEiendeler": eiendeler},
    }
    assert parse_key_figures([row])["ek_andel"] == expected


def test_parse_roles_real_hjr_json():
    # Brreg's answer as saved, except the chair: that name is made up.
    roles = parse_roles(json.loads((FIXTURES / "hjr_roller.json").read_text()))
    assert [(r["role"], r["name"], r["orgnr"]) for r in roles] == [
        ("Styrets leder", "Kari Nordmann", None),
        ("Revisor", "DELOITTE AS", "980211282"),
        ("Regnskapsfører", "ECIT NORIAN AS", "879906792"),
    ]


def test_parse_roles_skips_resigned():
    data = {
        "rollegrupper": [
            {
                "roller": [
                    {
                        "type": {"beskrivelse": "Daglig leder"},
                        "fratraadt": True,
                        "person": {"navn": {"fornavn": "Gammel", "etternavn": "Leder"}},
                    },
                    {
                        "type": {"beskrivelse": "Daglig leder"},
                        "fratraadt": False,
                        "person": {
                            "navn": {"fornavn": "Ny", "mellomnavn": "M", "etternavn": "Leder"}
                        },
                    },
                ]
            }
        ]
    }
    assert parse_roles(data) == [{"role": "Daglig leder", "name": "Ny M Leder", "orgnr": None}]


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([{"organisasjonsnummer": "998831067", "navn": "DIBBER AS"}], "998831067"),
        ([{"organisasjonsnummer": "923991832", "navn": "DIBBER INDIA AS"}], None),
        (
            [
                {"organisasjonsnummer": "998831067", "navn": "DIBBER AS"},
                {"organisasjonsnummer": "926052586", "navn": "Dibber AS."},
            ],
            None,
        ),
        ([], None),
    ],
    ids=["exact", "prefix-only", "ambiguous", "no-rows"],
)
async def test_find_exact(brreg, client_brreg, rows, expected):
    brreg.search_rows = rows
    assert await client_brreg.find_exact("Dibber AS") == expected


def test_parse_key_figures_without_selskap_row():
    assert parse_key_figures([{"regnskapstype": "KONSERN"}]) is None

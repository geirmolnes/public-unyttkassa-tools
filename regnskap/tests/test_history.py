"""Key figures for PDF-only years: values from the filing, verdicts from the next filing's prior column and the sums."""

import pytest

from src.accounts import checks, history, pipeline

LABEL = {
    "sum_driftsinntekter": "Sum driftsinntekter",
    "sum_driftskostnad": "Sum driftskostnader",
    "driftsresultat": "Driftsresultat",
    "sum_finansinntekter": "Sum finansinntekter",
    "sum_finanskostnad": "Sum finanskostnader",
    "netto_finans": "Netto finansposter",
    "res_for_skatt": "Ordinært resultat før skattekostnad",
    "skatt": "Skattekostnad på ordinært resultat",
    "aarsresultat": "Årsresultat",
    "sum_anleggsmidler": "Sum anleggsmidler",
    "sum_omlopsmidler": "Sum omløpsmidler",
    "sum_eiendeler": "Sum eiendeler",
    "sum_ek": "Sum egenkapital",
    "sum_langsiktig_gjeld": "Sum langsiktig gjeld",
    "sum_kortsiktig_gjeld": "Sum kortsiktig gjeld",
    "sum_gjeld": "Sum gjeld",
    "sum_ek_gjeld": "Sum egenkapital og gjeld",
}
RESULT = [
    "sum_driftsinntekter",
    "sum_driftskostnad",
    "driftsresultat",
    "sum_finansinntekter",
    "sum_finanskostnad",
    "netto_finans",
    "res_for_skatt",
    "skatt",
    "aarsresultat",
]
# In NOK 1000; every sum holds. Costs printed negative, as many filings do.
BASE = {
    "sum_driftsinntekter": 1000,
    "sum_driftskostnad": -800,
    "driftsresultat": 200,
    "sum_finansinntekter": 50,
    "sum_finanskostnad": -30,
    "netto_finans": 20,
    "res_for_skatt": 220,
    "skatt": -48,
    "aarsresultat": 172,
    "sum_anleggsmidler": 900,
    "sum_omlopsmidler": 300,
    "sum_eiendeler": 1200,
    "sum_ek": 500,
    "sum_langsiktig_gjeld": 600,
    "sum_kortsiktig_gjeld": 100,
    "sum_gjeld": 700,
    "sum_ek_gjeld": 1200,
}


def page(keys: list[str], cur: dict, prior: dict, scale: int, kind: str, extra: list = ()) -> dict:
    lines = [
        {
            "label": LABEL[k],
            "note_ref": None,
            "key": k,
            "current": cur.get(k),
            "prior": prior.get(k),
        }
        for k in keys
    ]
    lines += [
        {"label": lbl, "note_ref": None, "key": None, "current": c, "prior": p}
        for lbl, c, p in extra
    ]
    return {
        "page_kind": kind,
        "is_brreg_printout": False,
        "figures_for": "selskap",
        "unit_scale": scale,
        "current_year": None,
        "prior_year": None,
        "statement_lines": lines,
        "notes": [],
        "inline_amounts": [],
    }


def filing(
    cur: dict | None = None, prior: dict | None = None, scale: int = 1000, extra: list = ()
) -> list:
    cur, prior = {**BASE, **(cur or {})}, {**BASE, **(prior or {})}
    balance = [k for k in BASE if k not in RESULT]
    return [
        (3, page(RESULT, cur, prior, scale, "resultat")),
        (4, page(balance, cur, prior, scale, "balanse", extra)),
    ]


def build(docs: dict, pdf_years: set | None = None, brreg: dict | None = None) -> dict:
    return history.build(
        docs, "selskap", brreg or {}, pdf_years if pdf_years is not None else set(docs)
    )


def entry(out: dict, year: int) -> dict:
    return next(e for e in out["years"] if e["year"] == year)


def test_values_are_nok_with_costs_positive():
    e = entry(build({2020: filing()}), 2020)
    assert {
        f: e[f]
        for f in (
            "driftsinntekter",
            "driftskostnader",
            "finanskostnader",
            "skattekostnad",
            "egenkapital",
        )
    } == {
        "driftsinntekter": 1_000_000,
        "driftskostnader": 800_000,
        "finanskostnader": 30_000,
        "skattekostnad": 48_000,
        "egenkapital": 500_000,
    }


def test_tax_income_keeps_its_sign_from_the_result_lines():
    e = entry(build({2020: filing({"skatt": 48, "aarsresultat": 268})}), 2020)
    assert e["skattekostnad"] == -48_000


def test_ek_andel_and_period():
    e = entry(build({2020: filing()}), 2020)
    assert (e["ek_andel"], e["to_date"], e["pdf"]) == (
        pytest.approx(500 / 1200),
        "2020-12-31",
        True,
    )


def test_unkeyed_lines_found_by_label():
    e = entry(build({2020: filing(extra=[("Bankinnskudd, kontanter o.l.", 120, 90)])}), 2020)
    assert e["bankinnskudd"] == 120_000


@pytest.mark.parametrize(
    ("extra", "varer"),
    [
        (
            [
                ("Varelager", 4782, 5971),
                ("Finansielle omløpsmidler", 24803, 0),
                ("Sum varer", 29585, 5971),
            ],
            29585000,
        ),
        ([("Varelager", 4782, 5971)], 4782000),
    ],
    ids=["sum-beats-first-item", "item-alone"],
)
def test_section_sum_beats_its_first_item(extra, varer):
    """Regression: HJR Holding konsern 2025 read 'Varelager' 4 782 as varer; Brreg's 'Sum varer' is 29 585."""
    assert entry(build({2020: filing(extra=extra)}), 2020)["varer"] == varer


def test_company_copy_fills_what_the_printout_lacks():
    """Regression: HJR Holding konsern 2022: Brreg's printout stopped after the assets; equity and debt, only in the
    company's own copy, showed as not found."""
    assets = ["sum_anleggsmidler", "sum_omlopsmidler", "sum_eiendeler"]
    printout = [(n, {**d, "is_brreg_printout": True}) for n, d in filing()]
    printout[1][1]["statement_lines"] = [
        line for line in printout[1][1]["statement_lines"] if line["key"] in assets
    ]
    copy = [(n + 20, d) for n, d in filing()]
    e = entry(build({2020: printout + copy}), 2020)
    assert (
        e["egenkapital"],
        e["sum_gjeld"],
        e["checks"]["egenkapital"]["src"][0]["page"],
        e["missing"],
    ) == (
        500_000,
        700_000,
        24,
        [],
    )


def test_brreg_years_are_compared_on_the_printout_only():
    """The copy-only figures of a year Brreg has aren't compared: HJR konsern 2025 keyed total equity in the copy as
    innskutt egenkapital, and Brreg's figure (shown) was flagged."""
    printout = [(n, {**d, "is_brreg_printout": True}) for n, d in filing()]
    printout[1][1]["statement_lines"] = [
        line for line in printout[1][1]["statement_lines"] if line["key"] == "sum_eiendeler"
    ]
    copy = [(n + 20, d) for n, d in filing({"sum_ek": 999})]
    brreg = {2020: {"egenkapital": 500_000, "sum_eiendeler": 1_200_000}}
    out = build({2020: printout + copy}, brreg=brreg)
    assert sorted(out["brreg"][2020]) == ["sum_eiendeler"]


def test_older_printouts_sum_inntekter_and_sum_kostnader_are_operating_totals():
    """Regression: HJR Holding 2022's Brreg printout prints 'Sum inntekter' / 'Sum kostnader'; the read left them
    unkeyed and the table showed no Driftsinntekter or Driftskostnader."""
    keys = [k for k in RESULT if k not in ("sum_driftsinntekter", "sum_driftskostnad")]
    p = page(
        keys,
        BASE,
        BASE,
        1,
        "resultat",
        [("Sum inntekter", 628762, 0), ("Sum kostnader", 1115224, 244608)],
    )
    doc = pipeline.assemble({"base": {2: p}}, {})
    e = entry(build({2022: doc}), 2022)
    assert (e["driftsinntekter"], e["driftskostnader"]) == (628762, 1115224)


@pytest.mark.parametrize(
    ("drop", "change", "expected"),
    [
        ([], {}, []),
        (["sum_driftsinntekter", "sum_driftskostnad"], {}, ["driftsinntekter", "driftskostnader"]),
        # Driftsresultat = −costs: no revenue line because there is no revenue
        (["sum_driftsinntekter"], {"driftsresultat": -800}, []),
        # Sum gjeld = kortsiktig: no long-term debt
        (["sum_langsiktig_gjeld"], {"sum_gjeld": 100, "sum_ek": 1100}, []),
        (["sum_kortsiktig_gjeld"], {}, ["kortsiktig_gjeld"]),
    ],
    ids=[
        "all-found",
        "two-not-found",
        "zero-revenue",
        "no-long-term-debt",
        "short-term-debt-not-found",
    ],
)
def test_missing_rows_unless_a_sum_puts_them_at_zero(drop, change, expected):
    cur = {k: v for k, v in {**BASE, **change}.items() if k not in drop}
    doc = filing()
    for _, d in doc:
        d["statement_lines"] = [
            {**line, "current": cur.get(line["key"])}
            for line in d["statement_lines"]
            if line["key"] not in drop
        ]
    assert entry(build({2020: doc}), 2020)["missing"] == expected


@pytest.mark.parametrize(
    ("label", "key", "expected"),
    [
        ("Sum inntekter", None, "sum_driftsinntekter"),
        ("SUM KOSTNADER", None, "sum_driftskostnad"),
        ("Sum finansinntekter", None, None),
        ("Sum kostnader og avskrivninger", None, None),
        ("Sum kostnader", "sum_gjeld", "sum_gjeld"),  # the read's own key wins
    ],
)
def test_key_by_label(label, key, expected):
    line = {"label": label, "note_ref": None, "key": key, "current": 1, "prior": None}
    [(_, d)] = checks.key_by_label([(1, {"statement_lines": [line]})])
    assert d["statement_lines"][0]["key"] == expected


def test_key_by_label_keys_once_per_page():
    lines = [
        {"label": "Sum kostnader", "note_ref": None, "key": None, "current": v, "prior": None}
        for v in (1, 2)
    ]
    [(_, d)] = checks.key_by_label([(1, {"statement_lines": lines})])
    assert [line["key"] for line in d["statement_lines"]] == ["sum_driftskostnad", None]


@pytest.mark.parametrize(
    ("prior_in_next", "st"),
    [
        (1000, "ok"),  # same
        (1001, "ok"),  # one unit off = NOK 1000 rounding, the tolerance
        (1002, "restated"),  # just past it; this year's sums hold
    ],
    ids=["equal", "at-tolerance", "past-tolerance"],
)
def test_next_filings_prior_column_decides(prior_in_next, st):
    out = build({2020: filing(), 2021: filing(prior={"sum_driftsinntekter": prior_in_next})})
    assert entry(out, 2020)["checks"]["driftsinntekter"]["st"] == st


@pytest.mark.parametrize(
    ("factor", "prior_in_next", "st"),
    [
        (1000, 1_001_000, "ok"),  # a kroner page of whole thousands rounds to 1000
        (1000, 1_002_000, "restated"),
        (1003, 1_004_000, "restated"),  # amounts in kroner: 1000 apart is a real difference
    ],
    ids=["thousands-1000-apart", "thousands-2000-apart", "kroner-1000-apart"],
)
def test_kroner_page_of_whole_thousands_rounds_to_thousands(factor, prior_in_next, st):
    """Regression: HJR Holding konsern 2022 vs 2023's prior column, Brreg printouts in NOK of statements kept in
    NOK 1000: 6 530 411 000 against 6 530 410 000 was 'omarbeidet'."""
    kroner = {k: v * factor for k, v in BASE.items()}
    out = build(
        {
            2020: filing(kroner, kroner, scale=1),
            2021: filing(kroner, {**kroner, "sum_driftsinntekter": prior_in_next}, scale=1),
        }
    )
    assert entry(out, 2020)["checks"]["driftsinntekter"]["st"] == st


def test_difference_with_a_failing_sum_is_to_check():
    # driftsinntekter misread as 1100: inntekter − kostnader no longer gives driftsresultat
    out = build({2020: filing({"sum_driftsinntekter": 1100}), 2021: filing()})
    assert entry(out, 2020)["checks"]["driftsinntekter"]["st"] == "check"


@pytest.mark.parametrize(
    ("cur", "st"),
    [({}, "one"), ({"sum_driftsinntekter": 1100}, "check")],
    ids=["sums-hold", "sum-fails"],
)
def test_without_next_filing(cur, st):
    assert entry(build({2020: filing(cur)}), 2020)["checks"]["driftsinntekter"]["st"] == st


def test_sources_name_filing_column_and_page():
    out = build({2020: filing(), 2021: filing()})
    assert entry(out, 2020)["checks"]["sum_eiendeler"]["src"] == [
        {"year": 2020, "col": "current", "v": 1_200_000, "page": 4, "note": None},
        {"year": 2021, "col": "prior", "v": 1_200_000, "page": 4, "note": None},
    ]


@pytest.mark.parametrize(
    ("pdf_years", "has_prior_year"),
    [({2020}, True), ({2019, 2020}, False)],
    ids=["no-pdf-for-2019", "pdf-for-2019-exists"],
)
def test_year_before_oldest_filing_only_when_no_pdf_for_it(pdf_years, has_prior_year):
    out = build({2020: filing()}, pdf_years)
    assert (2019 in [e["year"] for e in out["years"]]) == has_prior_year


def test_year_before_oldest_filing_comes_from_its_prior_column():
    out = build({2020: filing(prior={"sum_driftsinntekter": 900, "driftsresultat": 100})}, {2020})
    c = entry(out, 2019)["checks"]["driftsinntekter"]
    assert (
        entry(out, 2019)["driftsinntekter"],
        c["st"],
        c["src"][0]["col"],
        c["src"][0]["year"],
    ) == (900_000, "one", "prior", 2020)


@pytest.mark.parametrize(
    ("brreg_value", "st"),
    [(1_000_000, "ok"), (1_000_000 + 1000, "ok"), (1_000_000 + 1001, "check")],
)
def test_years_brreg_has_are_only_compared(brreg_value, st):
    out = build({2023: filing()}, brreg={2023: {"driftsinntekter": brreg_value}})
    assert (
        out["years"] != [] and 2023 in [e["year"] for e in out["years"]],
        out["brreg"][2023]["driftsinntekter"]["st"],
    ) == (False, st)


def test_newest_first():
    out = build({2019: filing(), 2020: filing(), 2021: filing()}, {2018, 2019, 2020, 2021})
    assert [e["year"] for e in out["years"]] == [2021, 2020, 2019]

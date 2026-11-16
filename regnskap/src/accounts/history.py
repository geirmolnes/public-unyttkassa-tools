"""Key figures for years read from the PDF, in Brreg's key-figure fields, each with its sources and a verdict.

A figure is printed in two filings: its own year's column and the next year's prior-year column. It may also be
in a note, and it takes part in the statements' sums. Years Brreg has as data keep Brreg's figures; for those
the read is only compared with Brreg.
"""

import re
from typing import Any

from src.accounts import checks, store
from src.accounts.pipeline import assemble, scoped

# Statement key → key-figure field (src.brreg._flatten)
FIELDS = {
    "sum_driftsinntekter": "driftsinntekter",
    "sum_driftskostnad": "driftskostnader",
    "lonnskostnad": "loennskostnad",
    "driftsresultat": "driftsresultat",
    "sum_finansinntekter": "finansinntekter",
    "sum_finanskostnad": "finanskostnader",
    "netto_finans": "netto_finans",
    "res_for_skatt": "resultat_for_skatt",
    "skatt": "skattekostnad",
    "aarsresultat": "aarsresultat",
    "sum_anleggsmidler": "anleggsmidler",
    "sum_omlopsmidler": "omlopsmidler",
    "sum_eiendeler": "sum_eiendeler",
    "sum_innskutt_ek": "innskutt_egenkapital",
    "sum_opptjent_ek": "opptjent_egenkapital",
    "sum_ek": "egenkapital",
    "sum_langsiktig_gjeld": "langsiktig_gjeld",
    "sum_kortsiktig_gjeld": "kortsiktig_gjeld",
    "sum_gjeld": "sum_gjeld",
}
# Lines the page read doesn't key, found by their standard label (regnskapsloven's oppstillingsplan)
LABELS = [
    ("salgsinntekter", re.compile(r"^salgsinntekt", re.I)),
    ("rentekostnad_konsern", re.compile(r"^rentekostnad.*konsern", re.I)),
    ("rentekostnader", re.compile(r"^annen rentekostnad", re.I)),
    ("goodwill", re.compile(r"^goodwill", re.I)),
    ("varer", re.compile(r"^(sum\s+)?varer\b|^varelager", re.I)),
    ("fordringer", re.compile(r"^sum fordringer", re.I)),
    ("investeringer", re.compile(r"^sum investeringer", re.I)),
    ("bankinnskudd", re.compile(r"^(sum\s+)?bankinnskudd", re.I)),
]
SUM_FIRST = re.compile(r"^\s*sum\b", re.I)
ROUND_MIN = 5  # amounts on a page before whole thousands count as its rounding
# Printed with either sign by layout; Brreg's fields hold them positive
COSTS = {
    "driftskostnader",
    "loennskostnad",
    "finanskostnader",
    "rentekostnad_konsern",
    "rentekostnader",
}
SIGN_FREE = COSTS | {"skattekostnad"}
SUMS = [  # total = Σ sign · term
    ("driftsresultat", [("driftsinntekter", 1), ("driftskostnader", -1)]),
    ("netto_finans", [("finansinntekter", 1), ("finanskostnader", -1)]),
    ("resultat_for_skatt", [("driftsresultat", 1), ("netto_finans", 1)]),
    ("aarsresultat", [("resultat_for_skatt", 1), ("skattekostnad", -1)]),
    ("sum_eiendeler", [("anleggsmidler", 1), ("omlopsmidler", 1)]),
    ("sum_gjeld", [("langsiktig_gjeld", 1), ("kortsiktig_gjeld", 1)]),
    ("sum_eiendeler", [("egenkapital", 1), ("sum_gjeld", 1)]),
]
# Rows the table always shows; a PDF year lacking one lists it as missing unless a sum puts it at zero
MAIN = [
    "driftsinntekter",
    "driftskostnader",
    "driftsresultat",
    "finansinntekter",
    "finanskostnader",
    "resultat_for_skatt",
    "skattekostnad",
    "aarsresultat",
    "anleggsmidler",
    "omlopsmidler",
    "sum_eiendeler",
    "egenkapital",
    "langsiktig_gjeld",
    "kortsiktig_gjeld",
    "sum_gjeld",
]

Col = dict[
    str, tuple[float, int | None, int]
]  # field → (NOK, page or None when composed, unit scale)
_cache: dict[str, tuple[tuple, dict]] = {}


def final_doc(sha: str, choices: dict[int, str]) -> checks.Doc:
    """A finished read's pages as chosen after the checks, units and group pages fixed."""
    reads = {"base": store.reads_for(sha, "base")}
    for variant in set(choices.values()):
        reads[variant] = store.reads_for(sha, variant)
    return checks.relabel_group_runs(checks.fix_unit_scales(assemble(reads, choices)))


def scope_doc(doc: checks.Doc, scope: str) -> checks.Doc:
    return doc if scope == "selskap" else scoped(doc, "konsern")


def precision(d: dict) -> int:
    """The page's rounding unit: its unit scale, or 1000 when a page in kroner prints only whole thousands
    (Brreg's printouts of statements kept in NOK 1000: HJR konsern 2022 vs 2023's prior column, 1 000 apart)."""
    scale = d["unit_scale"] or 1
    amounts = [v for line in d["statement_lines"] for v in (line["current"], line["prior"]) if v]
    if scale == 1 and len(amounts) >= ROUND_MIN and all(v % 1000 == 0 for v in amounts):
        return 1000
    return scale


def column(doc: checks.Doc, col: int, fill: bool = True) -> Col:
    """One statement column (0 this year, 1 last year) as key-figure fields in NOK, costs positive. The third value
    is the rounding unit the comparisons allow."""
    out: Col = {}
    unit = {p: precision(d) for p, d in doc}
    gp = checks.groups(doc)
    vals = checks.primary_vals(doc)
    if fill and gp.get("brreg") and gp.get("company_e"):
        # The company's own copy fills what the printout lacks (HJR konsern 2022: equity and debt only in the copy)
        copy = gp["company_e"]
        vals = {**checks.derive_long_term_debt(checks.statement_vals(copy), copy), **vals}
    for key, (cur, prior, scale, page, _label) in vals.items():
        v = (cur, prior)[col]
        if key in FIELDS and v is not None:
            calc = page == checks.CALCULATED
            out[FIELDS[key]] = (v * scale, None if calc else page, scale if calc else unit[page])
    pages = gp.get("brreg") or gp.get("company_paper") or gp.get("company_e") or []
    lines = [(page, unit[page], line) for page, d in pages for line in d["statement_lines"]]
    # A section's 'Sum …' line beats its first item (HJR konsern 2025: 'Varelager' 4 782 before 'Sum varer' 29 585)
    lines.sort(key=lambda x: not SUM_FIRST.match(x[2]["label"] or ""))
    scales = {p: d["unit_scale"] or 1 for p, d in pages}
    for page, prec, line in lines:
        v = line["current" if col == 0 else "prior"]
        if v is None:
            continue
        for field, pat in LABELS:
            if field not in out and pat.search(line["label"].strip()):
                out[field] = (v * scales[page], page, prec)
    for f in COSTS & out.keys():
        out[f] = (abs(out[f][0]), *out[f][1:])
    if "skattekostnad" in out:  # tax income is negative; the sign follows from the result lines
        v, page, scale = out["skattekostnad"]
        v = abs(v)
        if "resultat_for_skatt" in out and "aarsresultat" in out:
            d = out["resultat_for_skatt"][0] - out["aarsresultat"][0]
            if abs(abs(d) - v) <= scale:
                v = d
        out["skattekostnad"] = (v, page, scale)
    return out


def same(a: float, b: float, scale: int, field: str = "") -> bool:
    if field in SIGN_FREE:
        a, b = abs(a), abs(b)
    return abs(a - b) <= scale


def sums_hold(vals: Col) -> dict[str, bool]:
    """Per field taking part in a sum with every term present: whether all such sums hold. Subtracted terms may
    carry either sign, as the identity checks allow."""
    out: dict[str, bool] = {}
    for total, terms in SUMS:
        if total not in vals or any(t not in vals for t, _ in terms):
            continue
        tol = max(vals[f][2] for f in [total, *(t for t, _ in terms)]) * len(terms)
        plus = sum(vals[t][0] for t, s in terms if s > 0)
        minus = [vals[t][0] for t, s in terms if s < 0]
        ok = any(
            abs(c - vals[total][0]) <= tol
            for c in {plus - sum(minus), plus + sum(minus), plus - sum(abs(x) for x in minus)}
        )
        for f in [total, *(t for t, _ in terms)]:
            out[f] = out.get(f, True) and ok
    return out


def implied_zero(vals: Col, field: str) -> bool:
    """Whether a sum whose other terms are all present holds with `field` at 0 (no such line in the filing)."""
    for total, terms in SUMS:
        names = [total, *(t for t, _ in terms)]
        if field not in names or any(n not in vals for n in names if n != field):
            continue
        filled = {**vals, field: (0.0, None, 1)}
        if sums_hold({n: filled[n] for n in names}).get(field):
            return True
    return False


def missing(vals: Col) -> list[str]:
    """MAIN fields the read didn't find and no sum puts at zero."""
    return [f for f in MAIN if f not in vals and not implied_zero(vals, f)]


def note_values(doc: checks.Doc) -> dict[str, list[tuple[float, str | None, int]]]:
    """Keyed note rows of the scope's own pages: field → [(NOK, note number, page)]."""
    out: dict[str, list] = {}
    for page, d in doc:
        if not checks.own_figures(d):
            continue
        scale = d["unit_scale"] or 1
        for note in d["notes"]:
            for t in note["tables"]:
                for r in t["rows"]:
                    if r["key"] in FIELDS:
                        for v in r["values"]:
                            if v is not None:
                                out.setdefault(FIELDS[r["key"]], []).append(
                                    (v * scale, note["number"], page)
                                )
    return out


def src(year: int, col: str, v: float, page: int | None, note: str | None = None) -> dict:
    return {"year": year, "col": col, "v": v, "page": page, "note": note}


def year_entry(year: int, own: Col, nxt: Col | None, notes: dict, next_year: int) -> dict[str, Any]:
    """Key figures for one PDF year with per-field checks. st: ok (two filings agree), restated (they differ and
    this year's sums hold), check (they differ or a sum fails without a second filing to settle it), one (only one
    filing so far)."""
    hold = sums_hold(own)
    out: dict[str, Any] = {f: v for f, (v, _, _) in own.items()}
    fchecks = {}
    for f, (v, page, scale) in own.items():
        sources = [src(year, "current", v, page)]
        note = next((n for n in notes.get(f, []) if checks.same_amount(v, n[0], scale)), None)
        if note:
            sources.append(src(year, "note", note[0], note[2], note[1]))
        if nxt is not None and f in nxt:
            other = nxt[f]
            sources.append(src(next_year, "prior", other[0], other[1]))
            if same(v, other[0], max(scale, other[2]), f):
                st = "ok"
            else:
                st = "check" if hold.get(f) is False else "restated"
        else:
            st = "check" if hold.get(f) is False else "one"
        fchecks[f] = {"st": st, "src": sources, "sum": hold.get(f)}
    out["checks"] = fchecks
    out["missing"] = missing(own)
    return out


def finish_entry(e: dict[str, Any], year: int, currency: str) -> dict[str, Any]:
    """Fill the fields the table derives (EK-andel, period) like src.brreg._flatten does."""
    e.update(year=year, from_date=None, to_date=f"{year}-12-31", currency=currency, pdf=True)
    se, ek = e.get("sum_eiendeler"), e.get("egenkapital")
    e["ek_andel"] = ek / se if se and ek is not None else None
    return e


def build(
    docs: dict[int, checks.Doc],
    scope: str,
    brreg: dict[int, dict[str, Any]],
    pdf_years: set[int],
    currency: str = "NOK",
) -> dict[str, Any]:
    """{years: entries newest first for read years Brreg lacks as data (plus the year before the oldest filing when
    no PDF exists for it, from that filing's prior column), brreg: {year: {field: check}} for read years Brreg has}.
    `docs` are final docs per filing year, `brreg` Brreg's key figures per year for this scope."""
    cols = {
        y: (column(scope_doc(d, scope), 0), column(scope_doc(d, scope), 1)) for y, d in docs.items()
    }
    years, against = [], {}
    for y in sorted(docs, reverse=True):
        own = cols[y][0]
        if not own:
            continue
        if y in brreg:
            # The printout's own figures only: Brreg's are shown, and a copy-only misread would flag them
            own = column(scope_doc(docs[y], scope), 0, fill=False)
            against[y] = {
                f: {
                    "st": "ok" if same(v, brreg[y][f], scale, f) else "check",
                    "src": [src(y, "brreg", brreg[y][f], None), src(y, "current", v, page)],
                    "sum": None,
                }
                for f, (v, page, scale) in own.items()
                if brreg[y].get(f) is not None
            }
            continue
        nxt = cols[y + 1][1] if y + 1 in cols else None
        e = year_entry(y, own, nxt, note_values(scope_doc(docs[y], scope)), y + 1)
        years.append(finish_entry(e, y, currency))
    first = min(docs, default=None)
    if (
        first is not None
        and first - 1 not in pdf_years
        and first - 1 not in brreg
        and cols[first][1]
    ):
        e = year_entry(first - 1, cols[first][1], None, {}, first)
        for c in e["checks"].values():
            c["src"][0]["year"], c["src"][0]["col"] = first, "prior"
        years.append(finish_entry(e, first - 1, currency))
    return {"years": years, "brreg": against}


def read_docs(orgnr: str) -> tuple[tuple, dict[int, checks.Doc]]:
    """(signature of the finished reads, final doc per year). The signature changes when a year is (re)read."""
    rows = [r for r in store.status_rows(orgnr) if r["status"] == "ok"]
    sig = tuple((r["year"], r["finished_at"]) for r in rows)
    cached = _cache.get(orgnr)
    if cached and cached[0] == sig:
        return sig, cached[1]
    docs = {}
    for r in rows:
        ar = store.account_read(orgnr, r["year"])
        if ar and ar["sha256"]:
            docs[r["year"]] = final_doc(ar["sha256"], ar["choices"])
    _cache[orgnr] = (sig, docs)
    return sig, docs


def figures(
    orgnr: str, brreg: dict[str, list[dict[str, Any]]], pdf_years: list[int]
) -> dict[str, Any]:
    """Per scope ('selskap', 'konsern') what build() gives. `brreg` holds Brreg's key-figure history per scope."""
    _, docs = read_docs(orgnr)
    out = {}
    for scope in ("selskap", "konsern"):
        hist = brreg.get(scope) or []
        currency = (hist[0].get("currency") if hist else None) or "NOK"
        out[scope] = build(docs, scope, {h["year"]: h for h in hist}, set(pdf_years), currency)
    return out

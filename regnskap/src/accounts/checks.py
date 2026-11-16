"""Ground-truth-free checks on one filing's page reads (ported from brreg_parse_bench/checks.py).

A document is `Doc = list[tuple[int, dict]]`: (1-based page number, page read as dict). Number lists (an
independent flat list of every number on each page) come in as {page: [numbers]}. Every failure names the pages
it implicates, so the repair step knows what to re-read.
"""

import itertools
import re
from typing import Any

Doc = list[tuple[int, dict]]
Vals = dict[str, tuple]  # key → (current, prior, unit_scale, page, label)

CALCULATED = -1  # page marker for values computed from other totals instead of read from a page
TOL = 2  # rounding in statements presented in NOK 1000
SIGN_FREE = {
    "sum_driftskostnad",
    "sum_finanskostnad",
    "skatt",
    "lonnskostnad",
}  # printed ± by layout
TAX_RATES = (0.22, 0.23, 0.24, 0.25, 0.27, 0.28)  # Norwegian corporate tax rates over the years
COMBO_SIGNS = (0, 1, -1)
COMBO_NEAREST = 8
SUBTOTAL_POOL = 8
TAIL_LINES = 2  # plain lines between the last sub-total and a total that may join the sub-totals
SECTION_WINDOW = 20
MIN_LISTED = 10  # single digits are note refs and counts

IDENTITIES = [
    ("balanse: eiendeler = EK+gjeld", [("sum_eiendeler", 1)], "sum_ek_gjeld"),
    (
        "anlegg + omløp = eiendeler",
        [("sum_anleggsmidler", 1), ("sum_omlopsmidler", 1)],
        "sum_eiendeler",
    ),
    ("EK + gjeld = sum EK og gjeld", [("sum_ek", 1), ("sum_gjeld", 1)], "sum_ek_gjeld"),
    (
        "langsiktig + kortsiktig = gjeld",
        [("sum_langsiktig_gjeld", 1), ("sum_kortsiktig_gjeld", 1)],
        "sum_gjeld",
    ),
    ("innskutt + opptjent = EK", [("sum_innskutt_ek", 1), ("sum_opptjent_ek", 1)], "sum_ek"),
    (
        "inntekter − kostnader = driftsres.",
        [("sum_driftsinntekter", 1), ("sum_driftskostnad", -1)],
        "driftsresultat",
    ),
    (
        "driftsres. + netto finans = res. før skatt",
        [("driftsresultat", 1), ("netto_finans", 1)],
        "res_for_skatt",
    ),
    ("res. før skatt − skatt = årsresultat", [("res_for_skatt", 1), ("skatt", -1)], "aarsresultat"),
]
BALANCE_IDENTITIES = [  # a + b = total
    ("sum_anleggsmidler", "sum_omlopsmidler", "sum_eiendeler"),
    ("sum_innskutt_ek", "sum_opptjent_ek", "sum_ek"),
    ("sum_langsiktig_gjeld", "sum_kortsiktig_gjeld", "sum_gjeld"),
    ("sum_ek", "sum_gjeld", "sum_ek_gjeld"),
]

SUBTOTAL_LABEL = re.compile(r"annen langsiktig|avsetning|other non-current|provisions", re.I)
TOTAL_LABEL = re.compile(r"\bsum|total|i alt", re.I)
PROVISIONS = re.compile(r"sum avsetning|total provisions", re.I)
PROVISION_ITEM = re.compile(
    r"^\s*(utsatt skatt\b(?!efordel)|pensjonsforpliktelse|andre avsetninger|deferred tax liabilit)",
    re.I,
)
OTHER_LT = re.compile(r"sum annen langsiktig|total other non-current", re.I)
# Totals the page read leaves unkeyed: Brreg's older printouts say 'Sum inntekter' / 'Sum kostnader' for the
# operating lines (HJR Holding 2022)
LABEL_KEYS = [
    ("sum_driftsinntekter", re.compile(r"^\s*sum inntekter\s*$", re.I)),
    ("sum_driftskostnad", re.compile(r"^\s*sum kostnader\s*$", re.I)),
]
# The result split between owners: 'Sum andel majoritet' is the result less the minority line below it
ALLOCATION = re.compile(
    r"andel (majoritet|minoritet)|(majoritetens|minoritetens|minoritets) andel|tilordnet|attributable|"
    r"non-controlling",
    re.I,
)
SUM_LINE = re.compile(r"^\s*(sum\b|total\b|totalt\b)|i alt\s*$", re.I)
# Computed lines not labelled 'Sum': results, cash-flow nets, closing cash. 'Netto endring' only for cash:
# 'Netto endring i øvrige langsiktige fordringer' is an ordinary cash-flow line.
COMPUTED_LINE = re.compile(
    r"^\s*(netto kontantstr|netto (endring|økning|reduksjon)( i)? (kontant|likvid|bankinnskudd|betalingsmid)|"
    r"net cash|net (change|increase|decrease)\b[^,]*\bcash|driftsresultat|operating (profit|result)|"
    r"ordinært resultat|resultat (før|etter|av)|årsresultat|årsoverskudd|årsunderskudd|profit|loss|"
    r"kontanter og kontantekvivalenter (pr|per) 31)",
    re.I,
)


def close(a: int | None, b: int | None, tol: int = TOL) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def matches(v: int, other: float, scale: int, key: str = "") -> bool:
    """Same figure, allowing thousands rounding and either sign on lines printed ± by layout."""
    if key in SIGN_FREE:
        v, other = abs(v), abs(other)
    return close(v * scale, other, tol=scale) or close(v, other)


def identity_ok(vals: Vals, terms: list, result: str, col: int) -> bool | None:
    """None when inputs are missing. Either sign accepted on subtracted terms (presentation varies)."""
    if result not in vals or any(k not in vals for k, _ in terms):
        return None
    xs = [vals[k][col] for k, _ in terms]
    r = vals[result][col]
    if r is None or any(x is None for x in xs):
        return None
    base = sum(x for (_, s), x in zip(terms, xs, strict=True) if s > 0)
    neg = [x for (_, s), x in zip(terms, xs, strict=True) if s < 0]
    return any(
        close(c, r) for c in {base - sum(neg), base + sum(neg), base - sum(abs(x) for x in neg)}
    )


def trusted_tag(key: str, label: str) -> bool:
    """sum_* keys only on lines that say they are a total; the model sometimes tags a lone component."""
    return not key.startswith("sum_") or bool(TOTAL_LABEL.search(label or ""))


def key_by_label(doc: Doc) -> Doc:
    """Unkeyed statement lines keyed by LABEL_KEYS, once per key and page; pages without such a line unchanged."""
    out: Doc = []
    for p, d in doc:
        keyed = {line["key"] for line in d["statement_lines"]}
        lines = []
        for line in d["statement_lines"]:
            key = next(
                (k for k, pat in LABEL_KEYS if pat.search(line["label"] or "")),
                None,
            )
            if line["key"] is None and key and key not in keyed:
                line = {**line, "key": key}
                keyed.add(key)
            lines.append(line)
        out.append((p, d if lines == d["statement_lines"] else {**d, "statement_lines": lines}))
    return out


def total_consistent(above: list[int], total: int, tax: bool = True) -> bool:
    """Total explained by the rows near it: a ± combination, the plain sum, an equal row (either sign: an operating
    result with no income is −costs), or row × tax rate."""
    nonzero = [x for x in above if x]
    if close(sum(nonzero), total) or any(close(abs(x), abs(total)) for x in nonzero[-3:]):
        return True
    if tax and any(abs(abs(x * r) - abs(total)) <= 1 for x in nonzero[-3:] for r in TAX_RATES):
        return True
    rows = nonzero[-COMBO_NEAREST:]
    return any(
        close(sum(s * x for s, x in zip(signs, rows, strict=True)), total)
        for signs in itertools.product(COMBO_SIGNS, repeat=len(rows))
        if sum(map(abs, signs)) >= 2
    )


def note_total_results(d: dict):
    """(row label, ok) per non-zero 'total' cell with ≥ 2 other non-zero cells in its column (above or below)."""
    for note in d["notes"]:
        for t in note["tables"]:
            ncols = max((len(r["values"]) for r in t["rows"]), default=0)
            for j in range(ncols):
                col = [r["values"][j] if j < len(r["values"]) else None for r in t["rows"]]
                for i, r in enumerate(t["rows"]):
                    v = col[i]
                    others = [(abs(k - i), x) for k, x in enumerate(col) if k != i and x]
                    if r["role"] != "total" or not v or len(others) < 2:
                        continue
                    # farthest → nearest, so total_consistent's "last n" means nearest n
                    yield (
                        r["label"],
                        total_consistent([x for _, x in sorted(others, key=lambda o: -o[0])], v),
                    )


def statement_vals(pages: Doc) -> Vals:
    """First keyed statement line per key."""
    vals: Vals = {}
    for page_no, d in pages:
        for line in d["statement_lines"]:
            k = line["key"]
            if not k or k in vals or not trusted_tag(k, line["label"]):
                continue
            if line["current"] is not None or line["prior"] is not None:
                vals[k] = (
                    line["current"],
                    line["prior"],
                    d["unit_scale"] or 1,
                    page_no,
                    line["label"],
                )
    return vals


def derive_long_term_debt(vals: Vals, pages: Doc) -> Vals:
    """sum_langsiktig_gjeld when the statements print no overall line: composed from printed sub-totals (provisions
    + other long-term debt) so lang + kort = gjeld stays a real check; last resort gjeld − kort, marked CALCULATED."""
    lang = vals.get("sum_langsiktig_gjeld")
    if lang is not None and not SUBTOTAL_LABEL.search(lang[4] or ""):
        return vals
    parts, items = {}, {}
    for page_no, d in pages:
        for line in d["statement_lines"]:
            label = line["label"] or ""
            if line["current"] is None and line["prior"] is None:
                continue
            for name, pat in (("prov", PROVISIONS), ("other", OTHER_LT)):
                if pat.search(label) and name not in parts:
                    parts[name] = (line["current"], line["prior"], d["unit_scale"] or 1, page_no)
            if PROVISION_ITEM.search(label) and label.lower() not in items:
                items[label.lower()] = (
                    line["current"],
                    line["prior"],
                    d["unit_scale"] or 1,
                    page_no,
                )
    if "prov" not in parts and items and "other" in parts:
        for i, v in enumerate(items.values()):
            parts[f"item{i}"] = v
    if parts:
        cols = [
            sum(p[c] or 0 for p in parts.values())
            if any(p[c] is not None for p in parts.values())
            else None
            for c in (0, 1)
        ]
        first = next(iter(parts.values()))
        return {**vals, "sum_langsiktig_gjeld": (cols[0], cols[1], first[2], first[3], "composed")}
    if not all(k in vals for k in ("sum_gjeld", "sum_kortsiktig_gjeld")):
        return vals
    cols = []
    for col in (0, 1):
        g, k = vals["sum_gjeld"][col], vals["sum_kortsiktig_gjeld"][col]
        cols.append(g - k if g is not None and k is not None else None)
    return {
        **vals,
        "sum_langsiktig_gjeld": (cols[0], cols[1], vals["sum_gjeld"][2], CALCULATED, "calculated"),
    }


def identity_result(vals: Vals, terms: list, res: str, col: int) -> bool | None:
    """An identity proves nothing if one of its terms was calculated from the others."""
    keys = [k for k, _ in terms] + [res]
    if any(k in vals and vals[k][3] == CALCULATED for k in keys):
        return None
    return identity_ok(vals, terms, res, col)


def fill_from_identities(vals: Vals) -> Vals:
    """Solve a missing balance-sheet total from the other two in an identity (marked CALCULATED)."""
    out = dict(vals)
    for _ in range(2):
        for a, b, tot in BALANCE_IDENTITIES:
            present = [k for k in (a, b, tot) if k in out]
            if len(present) != 2:
                continue
            missing = ({a, b, tot} - set(present)).pop()
            cols = []
            for col in (0, 1):
                x = {k: out[k][col] for k in present}
                if any(v is None for v in x.values()):
                    cols.append(None)
                elif missing == tot:
                    cols.append(x[a] + x[b])
                else:
                    cols.append(x[tot] - x[b if missing == a else a])
            out[missing] = (cols[0], cols[1], out[present[0]][2], CALCULATED, "calculated")
    return out


def statement_runs(pages: Doc) -> list[Doc]:
    """Pages with keyed statement lines, split into runs of (nearly) consecutive pages ≈ one set of statements."""
    runs: list[Doc] = []
    for page_no, d in pages:
        if not any(line["key"] for line in d["statement_lines"]):
            continue
        if runs and page_no - runs[-1][-1][0] <= 2:
            runs[-1].append((page_no, d))
        else:
            runs.append([(page_no, d)])
    return runs


def pick_run(runs: list[Doc], reference: Vals) -> Doc:
    """The company's own statements: the run agreeing most with Brreg's printout, else whose totals add up best.
    Filings can bundle a parent's or group's report; labels alone don't separate them."""

    def score(run: Doc) -> tuple[int, int, int]:
        v = statement_vals(run)
        agree = sum(
            1
            for k, x in v.items()
            if k in reference
            and x[0] is not None
            and reference[k][0] is not None
            and matches(x[0], reference[k][0] * reference[k][2], x[2], k)
        )
        idents = sum(
            1
            for _, terms, res in IDENTITIES
            for col in (0, 1)
            if identity_result(v, terms, res, col)
        )
        return agree, idents, len(v)

    return max(runs, key=score) if runs else []


def own_figures(d: dict) -> bool:
    return d.get("figures_for", "selskap") == "selskap"


def same_amount(a: float, b: float, tol: float) -> bool:
    """Equal within `tol`, either sign, or equal but for a missing thousands unit: a note table in NOK 1000 whose
    page doesn't say so (HENT 2025: 'Lønnskostnader 1 489 252' against 1 489 252 000 in the statement)."""
    a, b = abs(a), abs(b)
    return (
        abs(a - b) <= tol
        or abs(a * 1000 - b) <= max(tol, 1000)
        or abs(a - b * 1000) <= max(tol, 1000)
    )


def fix_unit_scales(doc: Doc) -> Doc:
    """Statement pages read without a unit, in a run whose other pages are in thousands (or millions), take that
    unit when Brreg's printout agrees better with it, or when the printout shares no lines with it (Læringsverkstedet 2025:
    'NOK 1000' printed only on the first of three pages)."""
    copy = [(p, d) for p, d in doc if d["statement_lines"] and not d["is_brreg_printout"]]
    printed = line_index(
        [(p, d) for p, d in doc if d["statement_lines"] and d["is_brreg_printout"]]
    )
    new_scale: dict[int, int] = {}
    runs = statement_runs(copy)
    for p, d in copy:  # pages without keyed totals (cash flow) join the run just before them
        if not any(line["key"] for line in d["statement_lines"]):
            run = next((r for r in runs if r[0][0] <= p <= r[-1][0] + 3), None)
            if run is None:
                runs.append([(p, d)])
            else:
                run.append((p, d))
    for run in runs:
        top = max(d["unit_scale"] or 1 for _, d in run)
        for p, d in run:
            if top == 1 or (d["unit_scale"] or 1) != 1:
                continue
            if not printed:
                new_scale[p] = top
                continue
            as_read = as_top = 0
            for line in d["statement_lines"]:
                hit = printed.get(norm_label(line["label"]))
                if hit is None or line["current"] is None or hit[0]["current"] is None:
                    continue
                theirs = abs(hit[0]["current"] * hit[2])
                as_read += close(abs(line["current"]), theirs, hit[2])
                as_top += abs(abs(line["current"]) * top - theirs) <= max(top, hit[2])
            if as_top > as_read or as_top == as_read == 0:  # no shared lines: the run decides
                new_scale[p] = top
    return [(p, {**d, "unit_scale": new_scale[p]} if p in new_scale else d) for p, d in doc]


def relabel_group_runs(doc: Doc) -> Doc:
    """Company-copy statements the model labelled as the company's own but that match Brreg's konsern printout better
    than its company printout are the group's (HENT 2025: konsern statements on pages 26–30 read as 'selskap')."""
    ref_group = statement_vals(
        [(p, d) for p, d in doc if d["figures_for"] == "konsern" and d["is_brreg_printout"]]
    )
    if not ref_group:
        return doc
    ref_own = statement_vals([(p, d) for p, d in doc if own_figures(d) and d["is_brreg_printout"]])

    def agree(run: Doc, ref: Vals) -> int:
        v = statement_vals(run)
        return sum(
            1
            for k, x in v.items()
            if k in ref
            and x[0] is not None
            and ref[k][0] is not None
            and matches(x[0], ref[k][0] * ref[k][2], x[2], k)
        )

    own_copy = [(p, d) for p, d in doc if own_figures(d) and not d["is_brreg_printout"]]
    group_pages = {
        p
        for run in statement_runs(own_copy)
        if agree(run, ref_group) >= 2 and agree(run, ref_group) > agree(run, ref_own)
        for p, _ in run
    }
    return [(p, {**d, "figures_for": "konsern"} if p in group_pages else d) for p, d in doc]


def groups(doc: Doc) -> dict[str, Doc]:
    """Statement pages of the company's own accounts: 'brreg' (Brreg's printout) and the company's own copy
    ('company_e' in e-filed years next to a printout, 'company_paper' without one)."""
    own = [(p, d) for p, d in doc if own_figures(d)]
    brreg = [(p, d) for p, d in own if d["is_brreg_printout"] and d["statement_lines"]]
    company = [(p, d) for p, d in own if not (d["is_brreg_printout"] and d["statement_lines"])]
    g = {"brreg": brreg}
    g["company_e" if brreg else "company_paper"] = pick_run(
        statement_runs(company), statement_vals(brreg)
    )
    return {k: v for k, v in g.items() if v}


def extracted_numbers(d: dict) -> set[int]:
    vals = [v for line in d["statement_lines"] for v in (line["current"], line["prior"])]
    vals += [v for n in d["notes"] for t in n["tables"] for r in t["rows"] for v in r["values"]]
    vals += [a["value"] for a in d.get("inline_amounts", [])]
    return {abs(v) for v in vals if v is not None}


def listed(numbers: list[int] | None) -> set[int] | None:
    """A page's number list without single digits and years."""
    if numbers is None:
        return None
    return {abs(x) for x in numbers if abs(x) >= MIN_LISTED and not 1990 <= abs(x) <= 2035}


def omissions(d: dict, numbers: list[int] | None) -> tuple[set[int], int]:
    """(numbers on the page missing from the read, numbers listed)."""
    lst = listed(numbers)
    if lst is None:
        return set(), 0
    return lst - extracted_numbers(d), len(lst)


# Column headings / inline labels of identifiers the number list leaves out on purpose (org and account numbers)
ID_LABEL = re.compile(
    r"org(anisasjons)?\.?\s*-?\s*(nr|nummer)|organi[sz]ation\s*(no|number)|kontonummer|konto\s*nr|account\s*(no|number)",
    re.I,
)


def identifier_values(d: dict) -> set[int]:
    """Values on a page that are org or account numbers by their own label: cells under a heading like
    'Organisasjonsnummer', and inline numbers labelled so. Judged by label, not by digits: a real amount of
    800–999 million passes the org-number check digit about 1 time in 11."""
    out: set[int] = set()
    for note in d["notes"]:
        for t in note["tables"]:
            nv = max((len(r["values"]) for r in t["rows"]), default=0)
            cols = t["columns"][-nv:] if nv else []
            for i, col in enumerate(cols):
                if ID_LABEL.search(col or ""):
                    out |= {
                        abs(r["values"][i])
                        for r in t["rows"]
                        if i < len(r["values"]) and r["values"][i] is not None
                    }
    out |= {
        abs(a["value"]) for a in d.get("inline_amounts", []) if ID_LABEL.search(a["context"] or "")
    }
    return out


def unlisted(d: dict, numbers: list[int] | None) -> set[int]:
    """Read values (≥ 1000) missing from the independent number list: misread or invented. Org and account
    numbers are skipped (regression: every row of a subsidiaries table was flagged, Læringsverkstedet 2025)."""
    lst = listed(numbers)
    if lst is None:
        return set()
    return {x for x in extracted_numbers(d) - identifier_values(d) if x >= 1000 and x not in lst}


def norm_label(s: str) -> str:
    s = (s or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    return re.sub(r"[^a-z]", "", s)


def section_results(pages: Doc):
    """(label, page, col, ok) per non-zero statement 'Sum …' or result value: it must equal a ± combination of the
    nearest non-zero lines above, or of earlier sub-totals. Catches amounts read one row off. Lines of another
    statement don't count as parts (HENT 2025: the cash flow's first lines were checked against the balance sheet)."""
    lines = [(page_no, line) for page_no, d in pages for line in d["statement_lines"]]
    kinds = [d["page_kind"] for _, d in pages for _ in d["statement_lines"]]
    starts = [0] * len(lines)  # index of the first line of each line's statement
    for i in range(1, len(lines)):
        starts[i] = starts[i - 1] if kinds[i] == kinds[i - 1] else i
    is_sum = [bool(SUM_LINE.search(line["label"] or "")) for _, line in lines]
    computed = [
        s or bool(COMPUTED_LINE.search(line["label"] or ""))
        for s, (_, line) in zip(is_sum, lines, strict=True)
    ]
    for i, (page_no, line) in enumerate(lines):
        if not computed[i] or ALLOCATION.search(line["label"] or ""):
            continue
        for col, field in enumerate(("current", "prior")):
            v = line[field]
            if not v:
                continue
            first = starts[i]
            prev_sum = next((j for j in range(i - 1, first - 1, -1) if is_sum[j]), first - 1)
            between = [lines[j][1] for j in range(prev_sum + 1, i)]
            if between and all(x["current"] is None and x["prior"] is None for x in between):
                continue  # heading only: the section prints just its sum
            above = [
                x
                for x in (lines[j][1][field] for j in range(max(first, i - SECTION_WINDOW), i))
                if x
            ]
            if not above:
                continue
            ok = total_consistent(above, v, tax=False)
            if not ok:
                subs = [
                    lines[j][1][field]
                    for j in range(i - 1, first - 1, -1)
                    if computed[j] and lines[j][1][field]
                ]
                # Plain lines after the last sub-total take part too: net change in cash = the three nets + FX effect
                last = next((j for j in range(i - 1, first - 1, -1) if computed[j]), first - 1)
                tail = [x for x in (lines[j][1][field] for j in range(last + 1, i)) if x][
                    -TAIL_LINES:
                ]
                subs = tail + subs[: SUBTOTAL_POOL - len(tail)]
                ok = any(
                    close(abs(sum(s * x for s, x in zip(signs, subs, strict=True))), abs(v))
                    for signs in itertools.product(COMBO_SIGNS, repeat=len(subs))
                    if sum(map(abs, signs)) >= 2
                )
            yield line["label"], page_no, col, ok


def line_index(pages: Doc) -> dict[str, tuple]:
    """First statement line per normalised label → (line, page, scale); labels seen twice are dropped."""
    seen: set[str] = set()
    out: dict[str, tuple] = {}
    for page_no, d in pages:
        for line in d["statement_lines"]:
            lbl = norm_label(line["label"])
            if len(lbl) < 6:
                continue
            if lbl in seen:
                out.pop(lbl, None)
                continue
            seen.add(lbl)
            out[lbl] = (line, page_no, d["unit_scale"] or 1)
    return out


# Brreg's key figures (src.brreg._flatten, from the JSON Brreg makes of the e-filed form, in NOK) per statement key.
BRREG_FIELDS = {
    "sum_driftsinntekter": "driftsinntekter",
    "sum_driftskostnad": "driftskostnader",
    "driftsresultat": "driftsresultat",
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


def reference(key_figures: dict[str, Any] | None) -> dict[str, float]:
    """Brreg's figures for one year as statement key → NOK."""
    kf = key_figures or {}
    return {k: kf[f] for k, f in BRREG_FIELDS.items() if kf.get(f) is not None}


def compare(vals: Vals, ref: dict[str, float], col: int) -> list[tuple[str, int, float, int, str]]:
    """(key, read value in NOK, reference, page, label) per keyed value in column `col` (0 this year, 1 last year)
    that differs from `ref`. Values composed from other totals (CALCULATED) are not compared."""
    out = []
    for key, other in ref.items():
        x = vals.get(key)
        if not x or x[col] is None or x[3] == CALCULATED:
            continue
        if not matches(x[col], other, x[2], key):
            out.append((key, x[col] * x[2], other, x[3], x[4]))
    return out


def primary_vals(doc: Doc) -> Vals:
    """The filing's own figures as shown: Brreg's printout, else the company's copy."""
    gp = groups(doc)
    pages = gp.get("brreg") or gp.get("company_paper") or gp.get("company_e") or []
    return derive_long_term_debt(statement_vals(pages), pages)


def prior_differences(doc: Doc, prior: dict[str, float]) -> list[dict[str, Any]]:
    """Last year's column against last year's own filing (`prior`, NOK). Not counted as failures: a company may
    restate its comparative figures (omarbeidet), so a difference is information, not a misread."""
    return [
        {"key": k, "label": label, "read": read, "before": before, "page": page}
        for k, read, before, page, label in compare(primary_vals(doc), prior, 1)
    ]


def doc_failures(
    doc: Doc, numlists: dict[int, list[int]], ref: dict[str, float] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """(failing checks, number of checks run). Each failure has 'kind' and 'pages' (to re-read). `ref` is Brreg's
    figures for the filing's year (NOK), when Brreg has them: every copy's current column must match."""
    fails: list[dict[str, Any]] = []
    n = 0
    vals: dict[str, Vals] = {}
    gp = groups(doc)
    for g, pages in gp.items():
        v = derive_long_term_debt(statement_vals(pages), pages)
        vals[g] = v
        if ref:
            n += sum(1 for k in ref if k in v and v[k][0] is not None and v[k][3] != CALCULATED)
            for key, read, other, page, label in compare(v, ref, 0):
                fails.append(
                    {
                        "kind": "brreg_data",
                        "group": g,
                        "key": key,
                        "row": label,
                        "values": [read],
                        "brreg": other,
                        "pages": {page},
                    }
                )
        for label, page_no, col, ok in section_results(pages):
            n += 1
            if not ok:
                fails.append(
                    {"kind": "section", "group": g, "row": label, "col": col, "pages": {page_no}}
                )
        for name, terms, res in IDENTITIES:
            for col in (0, 1):
                r = identity_result(v, terms, res, col)
                n += r is not None
                if r is False:
                    keys = [k for k, _ in terms] + [res]
                    fails.append(
                        {
                            "kind": "identity",
                            "group": g,
                            "check": name,
                            "col": col,
                            "pages": {v[k][3] for k in keys},
                        }
                    )
    if "brreg" in vals and "company_e" in vals:
        for key, (bv, _, bs, bp, _) in vals["brreg"].items():
            c = vals["company_e"].get(key)
            if (
                bp == CALCULATED
                or not (c and c[0] is not None and bv is not None)
                or c[3] == CALCULATED
            ):
                continue
            n += 1
            if not matches(c[0], bv * bs, c[2], key):
                fails.append({"kind": "copies", "key": key, "pages": {bp, c[3]}})
        b_lines, c_lines = line_index(gp["brreg"]), line_index(gp["company_e"])
        for lbl, (bl, bpg, bs) in b_lines.items():
            if lbl not in c_lines:
                continue
            cl, cpg, cs = c_lines[lbl]
            for f in ("current", "prior"):
                if bl[f] is None or cl[f] is None:
                    continue
                n += 1
                if abs(abs(bl[f] * bs) - abs(cl[f] * cs)) > max(bs, cs):
                    fails.append({"kind": "copies_line", "row": bl["label"], "pages": {bpg, cpg}})
    primary = vals.get("brreg") or vals.get("company_paper") or vals.get("company_e") or {}
    for page_no, d in doc:
        nums = numlists.get(page_no)
        missing, n_listed = omissions(d, nums)
        n += n_listed  # fixed per page, so a re-read cannot "pass" by extracting less
        fails += [{"kind": "omission", "value": v, "pages": {page_no}} for v in sorted(missing)]
        fails += [
            {"kind": "unlisted", "value": v, "pages": {page_no}} for v in sorted(unlisted(d, nums))
        ]
        for label, ok in note_total_results(d):
            n += 1
            if not ok:
                fails.append({"kind": "note_total", "row": label, "pages": {page_no}})
        for note in d["notes"] if own_figures(d) else []:
            for t in note["tables"]:
                for r in t["rows"]:
                    if not r["key"] or r["key"] not in primary:
                        continue
                    cur, prev, scale, sp = primary[r["key"]][:4]
                    targets = [x * scale for x in (cur, prev) if x is not None]
                    vs = [x * (d["unit_scale"] or 1) for x in r["values"] if x is not None]
                    n += bool(targets and vs)
                    tol = max(scale, d["unit_scale"] or 1)
                    if (
                        targets
                        and vs
                        and not any(same_amount(a, b, tol) for a in vs for b in targets)
                    ):
                        fails.append(
                            {
                                "kind": "note_link",
                                "key": r["key"],
                                "row": r["label"],
                                "values": vs,
                                "statement": targets,
                                "pages": {page_no, sp},
                            }
                        )
    return fails, n

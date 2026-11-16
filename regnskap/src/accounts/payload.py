"""The reader view's data for one read company-year: statements, notes (text + tables in print order), other pages,
and the checks that still fail, in words a reader understands, each pointing at a page of the stored PDF."""

from typing import Any

from src.accounts import checks, store
from src.accounts.history import final_doc
from src.accounts.pipeline import STATEMENT_KINDS, failures, scoped
from src.accounts.text import assign_notes, interleave, text_numbers

STATEMENTS = [
    ("resultat", "Resultatregnskap"),
    ("balanse", "Balanse"),
    ("kontantstrom", "Kontantstrøm"),
]
COL_NAME = {0: "inneværende år", 1: "fjoråret"}


def _fmt(v: int) -> str:
    return ("−" if v < 0 else "") + f"{abs(v):,}".replace(",", " ")


def _tables(note: dict) -> list[dict]:
    return [{"columns": t["columns"], "rows": t["rows"]} for t in note["tables"]]


def _seq(blocks: list[dict], tables: list[dict], page: int) -> list[dict]:
    return [
        {"t": "text", "kind": blocks[i]["kind"], "text": blocks[i]["text"], "page": page}
        if k == "text"
        else {"t": "table", "table": tables[i], "page": page}
        for k, i in interleave(blocks, tables)
    ]


def _with_neighbours(doc: checks.Doc, run: checks.Doc, printout: bool) -> checks.Doc:
    """The chosen statement run plus the same copy's statement pages right after it. The run is picked by keyed
    totals, and a cash flow statement has none, so it would otherwise be left out."""
    if not run:
        return run
    first, last = run[0][0], run[-1][0]
    return [
        (p, d)
        for p, d in doc
        if d["page_kind"] in STATEMENT_KINDS
        and checks.own_figures(d)
        and d["is_brreg_printout"] == printout
        and first <= p <= last + 3
    ]


def _printout_page(
    printout: checks.Doc, cur: int | None, prior: int | None, scale: int
) -> int | None:
    """The printout page with a line of the same amounts, compared in kroner within the copy's rounding."""
    if cur is None:
        return None
    for p, d in printout:
        s = d["unit_scale"] or 1
        tol = max(scale, s)
        for line in d["statement_lines"]:
            if line["current"] is None or abs(line["current"] * s - cur * scale) > tol:
                continue
            if (
                prior is None
                or line["prior"] is None
                or abs(line["prior"] * s - prior * scale) <= tol
            ):
                return p
    return None


def statements(doc: checks.Doc) -> dict[str, Any]:
    """The company's own statement lines per kind, marked where Brreg's printout has the same figures. A kind the
    company's copy lacks comes from the printout (HENT 2025: the group balance sheet is only in Brreg's copy)."""
    gp = checks.groups(doc)
    main = gp.get("company_e") or gp.get("company_paper") or gp.get("brreg") or []
    from_printout = main is gp.get("brreg")
    printout = [] if from_printout else gp.get("brreg", [])
    main = _with_neighbours(doc, main, from_printout)
    out = {}
    for kind, title in STATEMENTS:
        pages, copy = [(p, d) for p, d in main if d["page_kind"] == kind], from_printout
        if not pages:
            pages, copy = [(p, d) for p, d in printout if d["page_kind"] == kind], True
        if not pages:
            continue
        d0 = pages[0][1]
        scale = d0["unit_scale"] or 1
        lines = [
            {
                "label": line["label"],
                "ref": line["note_ref"],
                "key": line["key"],
                "cur": line["current"],
                "prior": line["prior"],
                "page": p,
                "also": None
                if copy
                else _printout_page(printout, line["current"], line["prior"], scale),
            }
            for p, d in pages
            for line in d["statement_lines"]
        ]
        out[kind] = {
            "title": title,
            "lines": lines,
            "pages": sorted({p for p, _ in pages}),
            "unit_scale": scale,
            "current_year": d0["current_year"],
            "prior_year": d0["prior_year"],
            "copy": "brreg_printout" if copy else "company_copy",
        }
    return out


def notes_and_pages(doc: checks.Doc, texts: dict[int, dict]) -> tuple[list[dict], list[dict]]:
    """Notes (same number on one page merged, continued across pages) and the other non-statement pages."""
    kinds = {p: d["page_kind"] for p, d in doc}
    order = [p for p, _ in doc]
    raw = [texts.get(p, {}).get("blocks", []) for p in order]
    text = dict(zip(order, assign_notes(raw, [kinds[p] for p in order]), strict=True))
    notes: list[dict] = []
    misc: list[dict] = []
    open_note = None
    for p, d in doc:
        if d["page_kind"] in STATEMENT_KINDS:
            open_note = None
            continue
        if d["page_kind"] != "noter":
            open_note = None
            ts = [t for n in d["notes"] for t in _tables(n)]
            if ts or any(b["kind"] != "table" for b in text[p]):
                misc.append({"page": p, "kind": d["page_kind"], "seq": _seq(text[p], ts, p)})
            continue
        groups: list[dict] = []
        for n in d["notes"]:
            if groups and groups[-1]["number"] == n["number"]:
                groups[-1]["tables"] += _tables(n)
            else:
                groups.append({"number": n["number"], "title": n["title"], "tables": _tables(n)})
        if not groups and text[p]:  # text-only page continuing a note
            groups.append({"number": None, "title": "", "tables": []})
        for g in groups:
            blocks = [
                b
                for b in text[p]
                if b["note"] == g["number"] or (g["number"] is None and b.get("continued"))
            ]
            if g["number"] is None and open_note is not None:
                open_note["seq"] += _seq(blocks, g["tables"], p)
                open_note["pages"].append(p)
                continue
            heading = next((b["text"] for b in blocks if b["kind"] == "heading"), "Uten tittel")
            open_note = {
                "number": g["number"],
                "title": g["title"] or heading,
                "pages": [p],
                "for": d["figures_for"],
                "seq": _seq(blocks, g["tables"], p),
            }
            notes.append(open_note)
    return notes, misc


def _check(
    page: int | None,
    kind: str,
    text: str,
    values: list[int],
    short: str | None = None,
    row: str | None = None,
) -> dict:
    return {
        "page": page,
        "kind": kind,
        "text": text,
        "short": short,
        "values": values,
        "row": row,
        "note": None,
    }


def reader_checks(
    failures: list[dict], doc: checks.Doc, texts: dict[int, dict], orgnr: str
) -> list[dict]:
    """Failures after repair, in words. Cover and signature pages are skipped (hashes, dates, postcodes), and a
    number counts as present when it is in the page's transcribed text, where the reader can see it."""
    kinds = {p: d["page_kind"] for p, d in doc}
    ids = {p: checks.identifier_values(d) for p, d in doc}
    in_text = {
        p: {abs(n) for b in t["blocks"] for n in text_numbers(b["text"])} for p, t in texts.items()
    }
    by_page: dict[tuple[str, int], list[int]] = {}
    out: list[dict] = []
    for f in failures:
        pages = [p for p in f["pages"] if p > 0]
        page = pages[0] if pages else None
        if f["kind"] in ("omission", "unlisted"):
            v = f["value"]
            if (
                kinds.get(page) == "annet"
                or v == int(orgnr)
                or (f["kind"] == "unlisted" and v in ids.get(page, ()))
                or (f["kind"] == "omission" and v in in_text.get(page, ()))
            ):
                continue
            by_page.setdefault((f["kind"], page), []).append(v)
        elif f["kind"] == "note_link":
            note_page = next((p for p in pages if kinds.get(p) == "noter"), page)
            out.append(
                _check(
                    note_page,
                    "Ulikt resultatregnskapet",
                    f"Noten oppgir {_fmt(f['values'][0])} som «{f['row']}», regnskapet {_fmt(f['statement'][0])}. "
                    "Sjekk om det er samme tall.",
                    f["values"],
                    short=f"Regnskapet sier {_fmt(f['statement'][0])}. Sjekk om det er samme tall.",
                )
            )
        elif f["kind"] == "section":
            out.append(
                _check(
                    page,
                    "Summen går ikke opp",
                    f"«{f['row']}» ({COL_NAME[f['col']]}) er ikke summen av linjene over.",
                    [],
                    row=f["row"],
                    short="Er ikke summen av linjene over. Sammenlign med skannet.",
                )
            )
        elif f["kind"] == "identity":
            out.append(
                _check(
                    page,
                    "Regnskapet går ikke opp",
                    f"{f['check']} stemmer ikke ({COL_NAME[f['col']]}).",
                    [],
                )
            )
        elif f["kind"] in ("copies", "copies_line"):
            what = f.get("row") or f.get("key")
            out.append(
                _check(
                    page,
                    "Ulikt i Brregs utskrift",
                    f"«{what}» står med ulikt beløp i Brregs utskrift og i selskapets egen oppstilling.",
                    [],
                    row=f.get("row"),
                )
            )
        elif f["kind"] == "brreg_data":
            where = " (konsern)" if f.get("scope") == "konsern" else ""
            out.append(
                _check(
                    page,
                    "Ulikt Brregs tall",
                    f"«{f['row']}»{where} er lest som {_fmt(f['values'][0])}, men Brregs tall for året er "
                    f"{_fmt(f['brreg'])}. Sammenlign med skannet.",
                    [],
                    row=f["row"],
                    short=f"Brreg oppgir {_fmt(f['brreg'])}. Sammenlign med skannet.",
                )
            )
        elif f["kind"] == "note_total":
            out.append(
                _check(
                    page,
                    "Sum i note stemmer ikke",
                    f"«{f['row']}» er ikke summen av radene i tabellen.",
                    [],
                    row=f["row"],
                    short="Er ikke summen av radene. Sammenlign med skannet.",
                )
            )
    for (kind, page), values in by_page.items():
        nums = ", ".join(_fmt(v) for v in values)
        if kind == "omission":
            out.append(
                _check(
                    page,
                    "Ikke hentet ut",
                    f"{nums} står på siden, men kom ikke med i tallene her. Se skannet.",
                    values,
                )
            )
        else:
            out.append(
                _check(
                    page,
                    "Mulig feillest",
                    f"{nums} ble ikke funnet igjen da siden ble lest på nytt. Sammenlign med skannet.",
                    values,
                    short="Ble ikke funnet igjen ved ny lesing av siden. Sammenlign med skannet.",
                )
            )
    unique = {
        (c["page"], c["kind"], c["text"]): c for c in out
    }  # a note row read twice gives the same flag twice
    return sorted(unique.values(), key=lambda c: (c["page"] or 0, c["kind"]))


def _note_numbers(note: dict) -> set[int]:
    """Every number in a note: table cells, and numbers in its text also as thousands/millions ('kr 18 mill')."""
    out: set[int] = set()
    for s in note["seq"]:
        if s["t"] == "table":
            out |= {abs(v) for r in s["table"]["rows"] for v in r["values"] if v is not None}
        else:
            out |= {abs(n) * m for n in text_numbers(s["text"]) for m in (1, 1000, 1_000_000)}
    return out


def attach_notes(flags: list[dict], notes: list[dict]) -> None:
    """Give a flag the note (index into `notes`) it belongs to when its page holds several notes: the one whose
    tables or text contain the flagged number. Flags no note claims stay page-wide (note None)."""
    for c in flags:
        on_page = [i for i, n in enumerate(notes) if c["page"] in n["pages"]]
        if len(on_page) == 1:
            c["note"] = on_page[0]
            continue
        hits = [
            i
            for i in on_page
            if c["values"] and {abs(v) for v in c["values"]} & _note_numbers(notes[i])
        ]
        c["note"] = hits[0] if len(hits) == 1 else None


def prior_info(doc: checks.Doc, prior: dict[str, dict[str, float]], year: int) -> list[dict]:
    """Last year's column against last year's filing, per scope, in words. Information, not a check: comparatives
    are sometimes restated (omarbeidet), so nothing here says the read is wrong."""
    out = []
    for scope, ref in prior.items():
        pages = doc if scope == "selskap" else scoped(doc, "konsern")
        for d in checks.prior_differences(pages, ref):
            where = " (konsern)" if scope == "konsern" else ""
            out.append(
                _check(
                    d["page"],
                    "Ulikt fjorårets regnskap",
                    f"«{d['label']}»{where} for {year - 1} står som {_fmt(d['read'])} her, men {_fmt(d['before'])} i "
                    f"regnskapet for {year - 1}. Tallene kan være omarbeidet; se notene.",
                    [],
                    row=d["label"],
                )
            )
    return out


def detail(
    orgnr: str,
    year: int,
    refs: dict[str, dict[str, float]] | None = None,
    prior: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any] | None:
    """Everything the reader view shows for one finished read; None when there is none. `refs` are Brreg's figures
    for the year (checked), `prior` last year's figures (shown as information), per scope."""
    ar = store.account_read(orgnr, year)
    if not ar or ar["status"] != "ok" or not ar["sha256"]:
        return None
    sha = ar["sha256"]
    pdf = store.pdf_file(sha)
    doc = final_doc(sha, ar["choices"])
    texts = store.reads_for(sha, "text")
    notes, misc = notes_and_pages(doc, texts)
    # Checks rerun on view (≈0.3 s for 90 pages), so fixes to them reach years read before the fix
    nums = {p: r["numbers"] for p, r in store.reads_for(sha, "numlist").items()}
    fails, _ = failures(doc, nums, refs)
    flags = reader_checks(fails, doc, texts, orgnr)
    attach_notes(flags, notes)
    inline = [
        {"context": a["context"], "value": a["value"], "page": p, "kind": d["page_kind"]}
        for p, d in doc
        for a in d.get("inline_amounts", [])
        if a["value"] != int(orgnr)
    ]
    return {
        "orgnr": orgnr,
        "year": year,
        "doc": {
            "sha256": sha,
            "n_pages": pdf["n_pages"] if pdf else len(doc),
            "models": store.read_models(sha),
            "read_at": ar["finished_at"],
            "cost": round(ar["cost"], 4),
            "n_checks": ar["n_checks"],
        },
        "pages": [
            {
                "n": p,
                "kind": d["page_kind"],
                "printout": d["is_brreg_printout"],
                "for": d["figures_for"],
            }
            for p, d in doc
        ],
        "statements": statements(doc),
        "konsern": statements(scoped(doc, "konsern")),
        "notes": notes,
        "misc": misc,
        "inline": inline,
        "checks": flags,
        "info": prior_info(doc, prior or {}, year),
    }

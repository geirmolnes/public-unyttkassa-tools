"""Read one company-year's årsregnskap: fetch + store the PDF, read every page, check, re-read failing pages, store.

Order (as benchmarked in brreg_parse_bench): per page a structured page read, an independent number list and, on
pages that aren't statements, the running text → ground-truth-free checks → re-reads of implicated pages with varied
input → per page the read that fails fewest checks without running fewer → figures + searchable text.
Every model call is stored per (pdf sha256, page, variant), so a re-run only pays for what is missing.
"""

import asyncio
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from src import archive, events
from src.accounts import checks, render, schema, store
from src.accounts.llm import MissingKey, Reader
from src.accounts.text import assign_notes
from src.brreg import BrregClient, BrregUnavailable, NotAvailable, parse_key_figures_history

log = logging.getLogger(__name__)

STATEMENT_KINDS = {"resultat", "balanse", "kontantstrom"}
PAGES_IN_FLIGHT = 24
READ_FAILED = "Kunne ikke lese årsregnskapet. Prøv igjen senere."
MAX_PAGES_PER_CLICK = 1000  # new pages one "Les inn" may read (stored pages are free)
# Each process vouches for its unfinished reads every WATCH_S; one silent for STALE_S (a deploy or crash stopped it)
# is taken over and resumed from the pages already stored. 0 turns the watch off (tests).
WATCH_S = float(os.environ.get("ACCOUNTS_WATCH_S", "15"))
STALE_S = 60
MAX_ATTEMPTS = 3
GAVE_UP = "Innlesingen stoppet flere ganger. Prøv igjen."
# Konsern checks run on the group's pages alone; these kinds come from the statements and their notes, the rest
# (number lists, note sums) already ran on every page in the main pass.
KONSERN_KINDS = {"section", "identity", "copies", "copies_line", "note_link", "brreg_data"}
# Brreg's figures for the filing's year, per scope ("selskap", "konsern"), as checks.reference gives them.
Refs = dict[str, dict[str, float]]
REREADS: dict[str, dict[str, Any]] = {"fill": {}, "align": {}, "think": {"reasoning": "low"}}
# Inline contexts that describe the hint instead of the page: what a model writes when it obeys a list of numbers
# it cannot find ('Tall nevnt: 6 401'), or the prompt's own examples copied verbatim.
GENERIC_CONTEXT = re.compile(
    r"\b(tall|numbers?)\s+(nevnt|mentioned|listed)\b|previous extraction|number list|unlabell?ed|"
    r"referenced number|reference number|honorar til revisor kr 31 000|bundne midler kr 11 511",
    re.I,
)


class Doc:
    """One filing being read: its PDF, identity and the reader that calls the models."""

    def __init__(
        self, reader: Reader, orgnr: str, year: int, name: str, pdf: bytes, sha: str
    ) -> None:
        self.reader, self.orgnr, self.year, self.name, self.pdf, self.sha = (
            reader,
            orgnr,
            year,
            name,
            pdf,
            sha,
        )

    async def png(self, page: int) -> bytes:
        return await render.cached(
            self.pdf, page, store.page_cache_dir(self.sha) / f"p{page}.png", render.model_png
        )

    async def call(
        self,
        page: int,
        variant: str,
        prompt: str,
        model: type[BaseModel],
        version: str,
        reasoning: str = "none",
    ) -> dict:
        """Stored read for (page, variant), else a model call that gets stored."""
        if (have := store.get_read(self.sha, page, variant)) is not None:
            return have
        parsed, usage = await self.reader.read(await self.png(page), prompt, model, reasoning)
        out = parsed.model_dump()
        store.put_read(
            self.sha, page, variant, usage.model, version, usage.fallback, usage.cost, out
        )
        store.add_progress(self.orgnr, self.year, cost=usage.cost)
        return out

    def page_prompt(self, hint: str | None = None) -> str:
        return schema.page_prompt(self.name, self.orgnr, self.year, hint)


async def read_page(doc: Doc, page: int) -> None:
    """Page read + number list, then the running text unless the page is a statement."""
    base, _ = await asyncio.gather(
        doc.call(page, "base", doc.page_prompt(), schema.Page, schema.PAGE_PROMPT_VERSION),
        doc.call(page, "numlist", schema.NUMLIST_PROMPT, schema.NumberList, "numlist"),
    )
    if base["page_kind"] not in STATEMENT_KINDS:
        prompt = schema.text_prompt(doc.name, doc.orgnr, doc.year)
        await doc.call(page, "text", prompt, schema.PageText, schema.TEXT_PROMPT_VERSION)
    store.add_progress(doc.orgnr, doc.year, pages=1)


async def read_all_pages(doc: Doc, n_pages: int) -> None:
    slots = asyncio.Semaphore(PAGES_IN_FLIGHT)

    async def one(page: int) -> None:
        async with slots:
            await read_page(doc, page)

    await asyncio.gather(*(one(p) for p in range(1, n_pages + 1)))


def invented_inline(base_read: dict, cand_read: dict) -> bool:
    """A re-read adds inline amounts whose context describes the hint, not the page (or has no real word)."""
    base = {a["value"] for a in base_read.get("inline_amounts", [])}
    added = [a for a in cand_read.get("inline_amounts", []) if a["value"] not in base]
    return any(
        GENERIC_CONTEXT.search(a["context"] or "")
        or not re.search(r"[^\W\d_]{3,}", a["context"] or "")
        for a in added
    )


def scoped(doc: checks.Doc, scope: str) -> checks.Doc:
    """The pages with `scope`'s figures, relabelled as own figures so the company checks apply to them."""
    return [(p, {**d, "figures_for": "selskap"}) for p, d in doc if d["figures_for"] == scope]


def failures(
    doc: checks.Doc, nums: dict[int, list[int]], refs: Refs | None = None
) -> tuple[list[dict[str, Any]], int]:
    """The company's checks, plus the same statement checks on the group (konsern) figures when the filing has them.
    With `refs`, each scope's figures are also checked against Brreg's for the year."""
    refs = refs or {}
    doc = checks.relabel_group_runs(checks.fix_unit_scales(doc))
    fails, n = checks.doc_failures(doc, nums, refs.get("selskap"))
    group = scoped(doc, "konsern")
    if any(d["statement_lines"] for _, d in group):
        kf, kn = checks.doc_failures(group, {}, refs.get("konsern"))
        fails += [{**f, "scope": "konsern"} for f in kf if f["kind"] in KONSERN_KINDS]
        n += kn
    return fails, n


def assemble(reads: dict[str, dict[int, dict]], choice: dict[int, str]) -> checks.Doc:
    return checks.key_by_label(
        [
            (p, reads.get(choice.get(p, "base"), {}).get(p, d))
            for p, d in sorted(reads["base"].items())
        ]
    )


def choose(
    reads: dict[str, dict[int, dict]],
    nums: dict[int, list[int]],
    pages: set[int],
    refs: Refs | None = None,
) -> dict[int, str]:
    """Per implicated page the read with fewest failing checks, never one that runs fewer checks (it would 'win' by
    dropping them). Two greedy passes so page choices can interact. CPU-bound: run in a thread."""
    _, base_n = failures(assemble(reads, {}), nums, refs)
    choice: dict[int, str] = {}
    for _ in range(2):
        for pg in sorted(pages):
            options = []
            for c in ["base", *REREADS]:
                if c != "base" and (
                    pg not in reads.get(c, {}) or invented_inline(reads["base"][pg], reads[c][pg])
                ):
                    continue
                fails, n = failures(assemble(reads, {**choice, pg: c}), nums, refs)
                if n >= base_n or c == choice.get(pg, "base"):
                    options.append((len(fails), -n, c != "base", c))
            choice[pg] = min(options)[3]
    return {p: c for p, c in choice.items() if c != "base"}


async def repair(
    doc: Doc,
    reads: dict[str, dict[int, dict]],
    nums: dict[int, list[int]],
    refs: Refs | None = None,
) -> dict[int, str]:
    """Re-read pages implicated by failing checks (cover/signature pages excluded) and pick the best read per page."""
    base = reads["base"]
    fails, _ = failures(assemble(reads, {}), nums, refs)
    pages = {pg for f in fails for pg in f["pages"] if pg > 0 and base[pg]["page_kind"] != "annet"}
    if not pages:
        return {}
    align = {
        pg
        for f in fails
        if f["kind"] in ("section", "identity", "copies", "brreg_data")
        for pg in f["pages"]
    }

    async def reread(pg: int, variant: str) -> None:
        hint, reasoning = None, REREADS[variant].get("reasoning", "none")
        if variant == "fill":
            missing, _ = checks.omissions(base[pg], nums.get(pg))
            if missing:
                hint = schema.FILL_HINT.format(
                    nums=", ".join(f"{v:,}".replace(",", " ") for v in sorted(missing))
                )
        elif variant == "align" and pg in align:
            hint = schema.ALIGN_HINT
        reads.setdefault(variant, {})[pg] = await doc.call(
            pg, variant, doc.page_prompt(hint), schema.Page, schema.PAGE_PROMPT_VERSION, reasoning
        )

    await asyncio.gather(*(reread(pg, v) for pg in pages for v in REREADS))
    return await asyncio.to_thread(choose, reads, nums, pages, refs)


def figure_rows(sha: str, doc: checks.Doc) -> list[tuple]:
    """Every value of the chosen reads as (sha, source, figures_for, copy, note, label, key, col, value, scale, page)."""
    rows = []
    for page, d in doc:
        scale, who = d["unit_scale"] or 1, d["figures_for"]
        copy = "brreg_printout" if d["is_brreg_printout"] else "company_copy"
        for line in d["statement_lines"]:
            for col in ("current", "prior"):
                if line[col] is not None:
                    rows.append(
                        (
                            sha,
                            "statement",
                            who,
                            copy,
                            None,
                            line["label"],
                            line["key"],
                            col,
                            line[col],
                            scale,
                            page,
                        )
                    )
        for note in d["notes"]:
            for t in note["tables"]:
                nv = max((len(r["values"]) for r in t["rows"]), default=0)
                cols = t["columns"][-nv:] if nv else []
                for r in t["rows"]:
                    for i, v in enumerate(r["values"]):
                        if v is not None:
                            col = cols[i] if i < len(cols) else str(i)
                            rows.append(
                                (
                                    sha,
                                    "note",
                                    who,
                                    copy,
                                    note["number"],
                                    r["label"],
                                    r["key"],
                                    col,
                                    v,
                                    scale,
                                    page,
                                )
                            )
        for a in d.get("inline_amounts", []):
            rows.append(
                (sha, "inline", who, copy, None, a["context"], None, None, a["value"], 1, page)
            )
    return rows


def text_rows(base: dict[int, dict], texts: dict[int, dict]) -> list[tuple]:
    """Searchable (text, page, note, kind) for every transcribed block except table placeholders."""
    order = sorted(texts)
    blocks = assign_notes(
        [texts[p]["blocks"] for p in order], [base[p]["page_kind"] for p in order]
    )
    return [
        (b["text"], p, b["note"], b["kind"])
        for p, bs in zip(order, blocks, strict=True)
        for b in bs
        if b["kind"] != "table"
    ]


def over_budget(n_new: int, budget: list[int]) -> str | None:
    """Why this filing can't be read within what is left of the click's page budget; None when it fits."""
    if n_new <= budget[0]:
        return None
    if n_new > MAX_PAGES_PER_CLICK:
        return f"Ikke lest: {n_new} sider er mer enn grensen på {MAX_PAGES_PER_CLICK} sider per innlesing."
    return (
        f"Ikke lest: grensen er {MAX_PAGES_PER_CLICK} sider per innlesing, og dette året har {n_new} nye sider. "
        "Les det inn for seg."
    )


async def brreg_refs(client: BrregClient, orgnr: str, year: int) -> Refs:
    """Brreg's figures for `year` per scope, where Brreg has that year as data (the last few years) or we kept it
    from an earlier visit. Brreg being down leaves only what we kept; nothing at all skips the check."""
    out: Refs = {}
    try:
        live = await client.figure_rows(orgnr)
    except (BrregUnavailable, httpx.HTTPError):
        live = []
    rows = await asyncio.to_thread(archive.regnskap_rows, orgnr, live)
    for scope in ("selskap", "konsern"):
        hist = parse_key_figures_history(rows, scope.upper())
        row = next((r for r in hist if r["year"] == year), None)
        if ref := checks.reference(row):
            out[scope] = ref
    return out


async def read_year(
    client: BrregClient,
    reader: Reader,
    orgnr: str,
    year: int,
    name: str,
    budget: list[int] | None = None,
) -> None:
    """Read one filing end to end; status, progress and errors land in account_reads. `budget` holds the pages the
    click may still read and is drawn down by the pages this filing needs read (pages already stored are free)."""
    budget = budget if budget is not None else [MAX_PAGES_PER_CLICK]
    events.about(orgnr, year)
    started = time.monotonic()
    store.update(orgnr, year, status="running", phase="henter")
    events.record("read.start", name)
    try:
        pdf = await client.fetch_pdf(orgnr, year)
        n_pages = await render.count_pages(pdf)
        sha = store.content_hash(pdf)
        n_new = n_pages - store.count_reads(sha, "base")
        events.record(
            "read.pdf", f"{n_pages} sider, {n_new} nye", detail={"sha256": sha, "bytes": len(pdf)}
        )
        if why := over_budget(n_new, budget):
            store.fail(orgnr, year, why)
            events.record("read.failed", why, level="warning")
            return
        budget[0] -= n_new
        store.save_pdf(orgnr, year, pdf, n_pages)
        store.update(orgnr, year, sha256=sha, pages_total=n_pages, pages_done=0, phase="leser")
        doc = Doc(reader, orgnr, year, name, pdf, sha)
        await read_all_pages(doc, n_pages)
        store.update(orgnr, year, phase="kontrollerer")
        events.record(
            "read.phase", "kontrollerer", detail={"seconds": round(time.monotonic() - started)}
        )
        reads = {"base": store.reads_for(sha, "base")}
        nums = {p: r["numbers"] for p, r in store.reads_for(sha, "numlist").items()}
        refs = await brreg_refs(client, orgnr, year)
        choice = await repair(doc, reads, nums, refs)
        final = assemble(reads, choice)
        fails, n_checks = failures(final, nums, refs)
        store.replace_figures(orgnr, year, figure_rows(sha, final))
        store.replace_text(orgnr, year, text_rows(reads["base"], store.reads_for(sha, "text")))
        store.finish(orgnr, year, choice, fails, n_checks)
        row = store.account_read(orgnr, year) or {}
        events.record(
            "read.done",
            f"{len(fails)} merknader av {n_checks} kontroller",
            detail={
                "seconds": round(time.monotonic() - started),
                "cost": row.get("cost"),
                "rereads_chosen": len(choice),
            },
        )
        # model PNGs (~0.3 MB a page) are only needed while reading; viewer WebPs stay
        for png in store.page_cache_dir(sha).glob("p*.png"):
            png.unlink()
    except (MissingKey, NotAvailable) as e:
        store.fail(orgnr, year, str(e))
        events.record("read.failed", str(e), level="warning")
    except asyncio.CancelledError:
        # The process is shutting down (deploy); the row stays unfinished for the next process to resume
        events.record("read.interrupted", "prosessen stoppet", level="warning")
        raise
    except Exception:
        log.exception("reading accounts failed for %s/%s", orgnr, year)
        store.fail(orgnr, year, READ_FAILED)
        events.record("read.failed", READ_FAILED, level="error")


def start(
    client: BrregClient,
    reader: Reader,
    orgnr: str,
    name: str,
    years: list[int],
    tasks: set[asyncio.Task],
) -> list[int]:
    """Queue `years` (newest first) and read them one after another. Years already queued or running are skipped.
    Returns the years queued."""
    busy = {r["year"] for r in store.status_rows(orgnr) if r["status"] in ("queued", "running")}
    todo = sorted({y for y in years if y not in busy}, reverse=True)
    if not todo:
        return []
    reader.chain()  # raises MissingKey before anything is queued
    for y in todo:
        store.queue(orgnr, y, reader.chain()[0], name, events.PROCESS)
    run(client, reader, orgnr, name, todo, tasks)
    return todo


def run(
    client: BrregClient,
    reader: Reader,
    orgnr: str,
    name: str,
    years: list[int],
    tasks: set[asyncio.Task],
) -> None:
    """Read `years` one after another in a background task."""

    async def go() -> None:
        budget = [MAX_PAGES_PER_CLICK]
        for y in years:
            await read_year(client, reader, orgnr, y, name, budget)

    task = asyncio.create_task(go())
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def unfinished(owner: str | None = None) -> list[str]:
    """'orgnr/year (status, owner)' for unfinished reads, optionally only `owner`'s."""
    rows = store.stale("9999")  # every unfinished read
    return [
        f"{r['orgnr']}/{r['year']} ({r['phase'] or 'i kø'}, {r['owner']})"
        for r in rows
        if owner in (None, r["owner"])
    ]


def resume_stale(
    client: BrregClient, reader: Reader, tasks: set[asyncio.Task], stale_s: float = STALE_S
) -> list[tuple[str, int]]:
    """Take over reads no process has vouched for in `stale_s` and read them on from the stored pages. A read that
    has been taken over MAX_ATTEMPTS times fails instead (something keeps stopping it). Returns the reads resumed."""
    before = (datetime.now(UTC) - timedelta(seconds=stale_s)).isoformat()
    by_orgnr: dict[str, list[dict]] = {}
    for r in store.stale(before):
        if not store.claim(r["orgnr"], r["year"], events.PROCESS, r["heartbeat_at"]):
            continue
        detail = {k: r[k] for k in ("owner", "heartbeat_at", "phase", "pages_done", "pages_total")}
        if r["attempts"] + 1 > MAX_ATTEMPTS:
            store.fail(r["orgnr"], r["year"], GAVE_UP)
            events.record(
                "read.gave_up",
                GAVE_UP,
                level="error",
                orgnr=r["orgnr"],
                year=r["year"],
                detail=detail,
            )
            continue
        events.record(
            "read.resumed",
            f"tatt over (forsøk {r['attempts'] + 1}); forrige prosess: {r['owner'] or 'ukjent'}",
            level="warning",
            orgnr=r["orgnr"],
            year=r["year"],
            detail=detail,
        )
        by_orgnr.setdefault(r["orgnr"], []).append(r)
    for orgnr, rows in by_orgnr.items():
        name = next((r["name"] for r in rows if r["name"]), orgnr)
        run(client, reader, orgnr, name, sorted((r["year"] for r in rows), reverse=True), tasks)
    return [(o, r["year"]) for o, rows in by_orgnr.items() for r in rows]


async def watch(state: Any, tasks: set[asyncio.Task]) -> None:
    """Every WATCH_S: vouch for this process's reads, and resume reads a stopped process left behind.
    `state` is app.state (its brreg client and reader, read each time so tests can swap them)."""
    while True:
        try:
            store.heartbeat(events.PROCESS)
            resume_stale(state.brreg, state.reader, tasks)
        except Exception:
            log.exception("watching account reads failed")
        await asyncio.sleep(WATCH_S)

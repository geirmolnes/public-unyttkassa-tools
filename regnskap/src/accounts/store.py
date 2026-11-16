"""Storage for read årsregnskap: the PDF file, raw page reads, read status, figures and searchable text."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from src import db


def pdf_dir() -> Path:
    return db.data_dir() / "pdf"


def page_cache_dir(sha256: str) -> Path:
    return db.data_dir() / "pages" / sha256[:16]


# Brreg writes a fresh trailer /ID into the PDF on every download (seen 2026-10-08: two downloads a second apart
# differ only there), so the raw sha256 changes each time. Reads are keyed by the hash with /ID blanked instead.
TRAILER_ID = re.compile(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]")


def content_hash(pdf: bytes) -> str:
    """sha256 of the PDF with its trailer /ID blanked: equal for every download of the same filing."""
    return hashlib.sha256(TRAILER_ID.sub(b"/ID[]", pdf)).hexdigest()


def save_pdf(orgnr: str, year: int, pdf: bytes, n_pages: int) -> str:
    """Keep the bytes Brreg returned and record them; returns their content hash. Brreg can replace a filing,
    so every read is tied to the hash of the file it came from."""
    sha = content_hash(pdf)
    path = pdf_dir() / orgnr / f"{year}_{sha[:12]}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(pdf)
    db._run(
        "INSERT OR IGNORE INTO pdf_files (sha256, orgnr, year, path, n_pages, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sha, orgnr, year, str(path.relative_to(db.data_dir())), n_pages, db.now_iso()),
    )
    return sha


def pdf_file(sha256: str) -> dict[str, Any] | None:
    row = db._one("SELECT * FROM pdf_files WHERE sha256 = ?", (sha256,))
    return {**row, "abs_path": db.data_dir() / row["path"]} if row else None


def stored_pdf(orgnr: str, year: int) -> dict[str, Any] | None:
    """The PDF the latest finished read of this company-year used."""
    r = db._one(
        "SELECT sha256 FROM account_reads WHERE orgnr = ? AND year = ? AND sha256 IS NOT NULL",
        (orgnr, year),
    )
    return pdf_file(r["sha256"]) if r else None


# ---------- page reads ----------


def get_read(sha256: str, page: int, variant: str) -> dict | None:
    row = db._one(
        "SELECT read_json FROM page_reads WHERE sha256 = ? AND page = ? AND variant = ?",
        (sha256, page, variant),
    )
    return json.loads(row["read_json"]) if row else None


def put_read(
    sha256: str,
    page: int,
    variant: str,
    model: str,
    prompt_version: str,
    fallback: bool,
    cost: float,
    read: dict,
) -> None:
    db._run(
        "INSERT OR REPLACE INTO page_reads (sha256, page, variant, model, prompt_version, fallback, cost, read_json, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sha256,
            page,
            variant,
            model,
            prompt_version,
            int(fallback),
            cost,
            json.dumps(read, ensure_ascii=False),
            db.now_iso(),
        ),
    )


def count_reads(sha256: str, variant: str) -> int:
    return db._one(
        "SELECT COUNT(*) AS n FROM page_reads WHERE sha256 = ? AND variant = ?", (sha256, variant)
    )["n"]


def reads_for(sha256: str, variant: str) -> dict[int, dict]:
    rows = db._all(
        "SELECT page, read_json FROM page_reads WHERE sha256 = ? AND variant = ?", (sha256, variant)
    )
    return {r["page"]: json.loads(r["read_json"]) for r in rows}


def read_models(sha256: str) -> list[str]:
    return [
        r["model"]
        for r in db._all("SELECT DISTINCT model FROM page_reads WHERE sha256 = ?", (sha256,))
    ]


# ---------- read status ----------


def queue(
    orgnr: str, year: int, model: str, name: str | None = None, owner: str | None = None
) -> None:
    now = db.now_iso()
    db._run(
        "INSERT INTO account_reads (orgnr, year, status, model, started_at, name, owner, heartbeat_at, attempts) "
        "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT (orgnr, year) DO UPDATE SET status = 'queued', phase = NULL, pages_done = 0, "
        "pages_total = NULL, error = NULL, model = excluded.model, started_at = excluded.started_at, "
        "finished_at = NULL, name = excluded.name, owner = excluded.owner, heartbeat_at = excluded.heartbeat_at, "
        "attempts = 0",
        (orgnr, year, model, now, name, owner, now),
    )


def heartbeat(owner: str) -> None:
    """The process `owner` is still working on its unfinished reads."""
    db._run(
        "UPDATE account_reads SET heartbeat_at = ? WHERE owner = ? AND status IN ('queued', 'running')",
        (db.now_iso(), owner),
    )


def stale(before: str) -> list[dict[str, Any]]:
    """Unfinished reads whose process last vouched for them before `before` (or never did)."""
    return db._all(
        "SELECT orgnr, year, name, owner, heartbeat_at, attempts, phase, pages_done, pages_total "
        "FROM account_reads WHERE status IN ('queued', 'running') AND (heartbeat_at IS NULL OR heartbeat_at < ?) "
        "ORDER BY orgnr, year DESC",
        (before,),
    )


def claim(orgnr: str, year: int, owner: str, seen: str | None) -> bool:
    """Take over a stale read, unless another process got there first (its heartbeat is no longer `seen`)."""
    return (
        db._run(
            "UPDATE account_reads SET owner = ?, heartbeat_at = ?, attempts = attempts + 1, status = 'queued', "
            "phase = NULL WHERE orgnr = ? AND year = ? AND status IN ('queued', 'running') AND heartbeat_at IS ?",
            (owner, db.now_iso(), orgnr, year, seen),
        )
        == 1
    )


def recent_reads(since: str) -> list[dict[str, Any]]:
    """Reads started or finished since `since`, across companies, newest first."""
    return db._all(
        "SELECT orgnr, year, name, status, phase, pages_total, pages_done, cost, error, owner, heartbeat_at, "
        "attempts, started_at, finished_at FROM account_reads WHERE started_at >= ? OR finished_at >= ? "
        "ORDER BY COALESCE(finished_at, started_at) DESC",
        (since, since),
    )


def update(orgnr: str, year: int, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    db._run(
        f"UPDATE account_reads SET {cols} WHERE orgnr = ? AND year = ?",
        (*fields.values(), orgnr, year),
    )


def spent_since(since: str) -> float:
    """What reads started since `since` have cost so far (USD)."""
    row = db._one(
        "SELECT COALESCE(SUM(cost), 0) AS cost FROM account_reads WHERE started_at >= ?", (since,)
    )
    return row["cost"] if row else 0.0


def add_progress(orgnr: str, year: int, pages: int = 0, cost: float = 0.0) -> None:
    db._run(
        "UPDATE account_reads SET pages_done = pages_done + ?, cost = cost + ? WHERE orgnr = ? AND year = ?",
        (pages, cost, orgnr, year),
    )


def status_rows(orgnr: str) -> list[dict[str, Any]]:
    return db._all(
        "SELECT year, status, phase, pages_total, pages_done, cost, model, error, started_at, finished_at "
        "FROM account_reads WHERE orgnr = ? ORDER BY year DESC",
        (orgnr,),
    )


def account_read(orgnr: str, year: int) -> dict[str, Any] | None:
    row = db._one("SELECT * FROM account_reads WHERE orgnr = ? AND year = ?", (orgnr, year))
    if row:
        row["choices"] = {int(k): v for k, v in json.loads(row["choices"] or "{}").items()}
        row["failures"] = json.loads(row["failures"] or "[]")
    return row


def finish(
    orgnr: str, year: int, choices: dict[int, str], failures: list[dict], n_checks: int
) -> None:
    update(
        orgnr,
        year,
        status="ok",
        phase=None,
        finished_at=db.now_iso(),
        n_checks=n_checks,
        choices=json.dumps(choices),
        failures=json.dumps([{**f, "pages": sorted(f["pages"])} for f in failures]),
    )


def fail(orgnr: str, year: int, error: str) -> None:
    update(orgnr, year, status="failed", phase=None, error=error, finished_at=db.now_iso())


# ---------- figures + text ----------


def replace_figures(orgnr: str, year: int, rows: list[tuple]) -> None:
    """rows: (sha256, source, figures_for, copy, note, label, key, col, value, unit_scale, page)."""
    with db.connect() as conn:
        conn.execute("DELETE FROM figures WHERE orgnr = ? AND year = ?", (orgnr, year))
        conn.executemany(
            "INSERT INTO figures (orgnr, year, sha256, source, figures_for, copy, note, label, key, col, value, "
            "unit_scale, page) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(orgnr, year, *r) for r in rows],
        )


def statement_totals(orgnr: str, year: int, scope: str = "selskap") -> dict[str, float]:
    """Our read of a year's keyed statement totals (this year's column, NOK): Brreg's printout first, then the
    company's copy, first page first. Empty when the year isn't read."""
    rows = db._all(
        "SELECT key, value * unit_scale AS v FROM figures WHERE orgnr = ? AND year = ? AND source = 'statement' "
        "AND figures_for = ? AND col = 'current' AND key IS NOT NULL "
        "ORDER BY copy = 'company_copy', page",
        (orgnr, year, scope),
    )
    out: dict[str, float] = {}
    for r in rows:
        out.setdefault(r["key"], r["v"])
    return out


def replace_text(orgnr: str, year: int, rows: list[tuple]) -> None:
    """rows: (text, page, note, kind)."""
    with db.connect() as conn:
        conn.execute("DELETE FROM text_fts WHERE orgnr = ? AND year = ?", (orgnr, year))
        conn.executemany(
            "INSERT INTO text_fts (text, orgnr, year, page, note, kind) VALUES (?, ?, ?, ?, ?, ?)",
            [(t, orgnr, year, p, n, k) for t, p, n, k in rows],
        )

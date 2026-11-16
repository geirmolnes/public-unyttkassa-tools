"""Keep what Brreg sent for companies people open, in case Brreg stops serving it: the accounts JSON per year
(Brreg's /regnskap only covers the last few years), the unit and roles JSON whenever they change, and every
annual-accounts PDF fetched for any reason (a filing's PDF doesn't change; duplicates are skipped by hash)."""

import asyncio
import hashlib
import json
import logging
import sqlite3
from typing import Any

from src import db
from src.accounts import render, store

log = logging.getLogger(__name__)


def keep_json(conn: sqlite3.Connection, orgnr: str, source: str, data: Any, now: str) -> None:
    """One snapshot per distinct content; seeing it again only moves `last_seen`."""
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    sha = hashlib.sha256(text.encode()).hexdigest()
    conn.execute(
        "INSERT INTO brreg_snapshots (orgnr, source, sha256, data, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET last_seen = excluded.last_seen",
        (orgnr, source, sha, text, now, now),
    )


def _period(row: dict[str, Any]) -> tuple[str, str]:
    return row.get("regnskapstype") or "", (row.get("regnskapsperiode") or {}).get("tilDato") or ""


def keep_regnskap(
    conn: sqlite3.Connection, orgnr: str, rows: list[dict[str, Any]], now: str
) -> None:
    """Each year's /regnskap row, latest version per (type, period end); every version also as a snapshot."""
    for row in rows:
        kind, to_date = _period(row)
        if to_date:
            conn.execute(
                "INSERT INTO brreg_regnskap (orgnr, kind, to_date, data, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET data = excluded.data, "
                "last_seen = excluded.last_seen",
                (orgnr, kind, to_date, json.dumps(row, ensure_ascii=False), now, now),
            )
        keep_json(conn, orgnr, f"regnskap:{kind}:{to_date}", row, now)


def regnskap_rows(orgnr: str, live: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Brreg's rows now, plus kept rows for periods Brreg no longer serves (marked `_archived`)."""
    have = {_period(r) for r in live}
    kept = db._all("SELECT data FROM brreg_regnskap WHERE orgnr = ?", (orgnr,))
    old = [json.loads(r["data"]) for r in kept]
    return live + [{**r, "_archived": True} for r in old if _period(r) not in have]


def keep_company(
    orgnr: str, rows: list[dict[str, Any]], raw: dict[str, Any]
) -> list[dict[str, Any]]:
    """Store what was fetched for an opened company; returns the accounts rows to show (live + kept)."""
    now = db.now_iso()
    # One transaction: a commit per row made this most of a warm company open
    with db.connect() as conn:
        keep_regnskap(conn, orgnr, rows, now)
        for source, data in raw.items():
            keep_json(conn, orgnr, source, data, now)
    return regnskap_rows(orgnr, rows)


def _keep_pdf(orgnr: str, year: int, pdf: bytes) -> None:
    store.save_pdf(orgnr, year, pdf, render._locked(render.page_count, pdf))


async def keep_pdf(orgnr: str, year: int, pdf: bytes) -> None:
    """BrregClient's on_pdf hook. A file that won't open is logged and skipped; the caller still gets it."""
    try:
        await asyncio.to_thread(_keep_pdf, orgnr, year, pdf)
    except Exception:
        log.warning("could not keep PDF %s/%s", orgnr, year, exc_info=True)

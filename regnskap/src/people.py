"""Person search: everyone who has held shares in the register, found by any word of the name.

The register names people by name and birth year only, first name first ("OLA JACOB NORDBY"), and
the app reads a read-only copy, so a search for «nordby» can't use an index there. people.db, next
to regnskap.db, holds one row per (name, birth year) with an FTS5 index on the name. It's built from
the register in a background thread, and built again when the register's imports change."""

import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from src import db, register

log = logging.getLogger(__name__)

DB_NAME = "people.db"
LIMIT = 6
LIST_MAX = 500  # the full list of matches (/api/people)
COUNT_MAX = 1000  # counting stops here: «over 1 000»
CHECK_EVERY = 600  # seconds between looks at whether the register has changed
LAYOUT = 2  # bump when people.db's tables change, so it's built again

# One row per person: the newest year they hold shares, how many companies then, and the address
# that year (the max of "year|postcode|place|country" is the newest year's). People without a birth
# year (foreign holders, companies and people mixed) or without a name are left out.
SOURCE = """SELECT holder_name AS name, holder_birth_year AS birth_year, MAX(year) AS last_year,
    COUNT(DISTINCT CASE WHEN year = :newest THEN orgnr END) AS companies,
    MAX(year || '|' || COALESCE(holder_postcode, '') || '|' || COALESCE(holder_place, '') || '|'
        || COALESCE(country, '')) AS address
    FROM holdings
    WHERE holder_orgnr IS NULL AND holder_birth_year IS NOT NULL AND holder_name IS NOT NULL
    GROUP BY holder_name, holder_birth_year"""
# Rows are stored best first (still holding shares, then most companies), so the index's rowid
# order hands back the best matches without sorting every hit.
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    birth_year INTEGER NOT NULL,
    last_year INTEGER NOT NULL,
    companies INTEGER NOT NULL,
    postcode TEXT,
    place TEXT,
    country TEXT
);
CREATE VIRTUAL TABLE people_fts USING fts5(
    name, content='people', content_rowid='id',
    tokenize='unicode61 remove_diacritics 0', prefix='2 3'
);
"""

_lock = threading.Lock()
_thread: threading.Thread | None = None
_checked_at = 0.0


def path() -> Path:
    return db.data_dir() / DB_NAME


def _fingerprint(conn: sqlite3.Connection) -> str:
    """Changes whenever a register year is imported again or added, or LAYOUT changes."""
    rows = conn.execute("SELECT year, rows, imported_at FROM imports ORDER BY year").fetchall()
    return f"layout {LAYOUT};" + ";".join(f"{y}:{n}:{at}" for y, n, at in rows)


def _built_from() -> str | None:
    if not path().exists():
        return None
    try:
        with closing(sqlite3.connect(f"file:{path()}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'register'").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _stale() -> bool:
    """people.db is missing or was built from other register imports (False without a register)."""
    try:
        with closing(register._connect()) as conn:
            current = _fingerprint(conn)
    except register.Unavailable:
        return False
    return current != _built_from()


def build() -> int:
    """Build people.db from the register; returns the number of people. Writes a temporary file
    and swaps it in, so searches keep working on the old one meanwhile."""
    started = time.monotonic()
    target = path()
    tmp = target.with_name(f"{DB_NAME}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.unlink(missing_ok=True)
    with closing(register._connect()) as src, closing(sqlite3.connect(tmp)) as out:
        fingerprint = _fingerprint(src)
        years = register._years(src)
        if not years:
            raise register.Unavailable("register has no years")
        newest = years[-1]
        out.executescript(SCHEMA)
        out.execute(
            "CREATE TEMP TABLE raw (name, birth_year, last_year, companies, postcode, place, country)"
        )
        cur = src.execute(SOURCE, {"newest": newest})
        while chunk := cur.fetchmany(20_000):
            out.executemany(
                "INSERT INTO raw VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(r[0], r[1], r[2], r[3], *r[4].split("|", 3)[1:]) for r in chunk],
            )
        out.execute(
            """INSERT INTO people (name, birth_year, last_year, companies, postcode, place, country)
            SELECT name, birth_year, last_year, companies, NULLIF(postcode, ''), NULLIF(place, ''),
                NULLIF(country, '')
            FROM raw ORDER BY last_year = ? DESC, companies DESC, name, birth_year""",
            (newest,),
        )
        out.execute("CREATE INDEX people_name ON people (name)")  # exact names, see search()
        out.execute("INSERT INTO people_fts (people_fts) VALUES ('rebuild')")
        out.execute(
            "INSERT INTO meta VALUES ('register', ?), ('newest', ?)", (fingerprint, str(newest))
        )
        n = out.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        out.commit()
    os.replace(tmp, target)
    log.info("people index: %d people in %.0f s", n, time.monotonic() - started)
    return n


def _build_logged() -> None:
    try:
        build()
    except Exception:
        log.exception("people index build failed")


def ensure_fresh(wait: bool = False) -> None:
    """Build people.db if it's missing or the register has changed since: in a background thread,
    looking at the register at most every CHECK_EVERY seconds. With `wait`, finish any build
    under way and then build here if it's still needed."""
    global _thread, _checked_at
    if wait:
        with _lock:
            running = _thread
        if running:
            running.join()
        with _lock:
            _checked_at = time.monotonic()
            if _stale():
                build()
        return
    with _lock:
        if (_thread and _thread.is_alive()) or time.monotonic() - _checked_at < CHECK_EVERY:
            return
        _checked_at = time.monotonic()
        if not _stale():
            return
        _thread = threading.Thread(target=_build_logged, name="people-index", daemon=True)
        _thread.start()


def _match(q: str) -> str | None:
    """Every word of `q` as a word prefix: «hans sund» → "HANS"* "SUND"* (both must match)."""
    words = re.findall(r"\w+", q.upper())
    return " ".join(f'"{w}"*' for w in words) if words else None


PERSON_COLS = "id, name, birth_year, last_year, companies, postcode, place, country"


def search(q: str, limit: int = LIMIT) -> dict[str, Any]:
    """People whose name has a word starting with each word of `q`: anyone named exactly `q`
    first, then best first. `total` counts all of them up to COUNT_MAX (`capped` when there are
    more). `ready` is False while people.db is being built for the first time."""
    ensure_fresh()
    match = _match(q)
    if not path().exists():
        return {"people": [], "total": 0, "capped": False, "ready": False}
    if not match:
        return {"people": [], "total": 0, "capped": False, "ready": True}
    with closing(sqlite3.connect(f"file:{path()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        newest = int(conn.execute("SELECT value FROM meta WHERE key = 'newest'").fetchone()[0])
        # «ola haug» must find OLA HAUG even when hundreds of OLA HAUG… hold more.
        exact = conn.execute(
            f"SELECT {PERSON_COLS} FROM people WHERE name = ? ORDER BY id LIMIT ?",
            (" ".join(q.upper().split()), limit),
        ).fetchall()
        rest = conn.execute(
            f"""SELECT {PERSON_COLS} FROM people WHERE id IN (
                SELECT rowid FROM people_fts WHERE people_fts MATCH ? ORDER BY rowid LIMIT ?)
            ORDER BY id""",
            (match, limit),
        ).fetchall()
        total = conn.execute(
            """SELECT COUNT(*) FROM (
                SELECT rowid FROM people_fts WHERE people_fts MATCH ? LIMIT ?)""",
            (match, COUNT_MAX + 1),
        ).fetchone()[0]
    seen = {r["id"] for r in exact}
    rows = [*exact, *(r for r in rest if r["id"] not in seen)][:limit]
    return {
        "people": [
            {**{k: r[k] for k in r.keys() if k != "id"}, "current": r["last_year"] == newest}
            for r in rows
        ],
        "total": min(total, COUNT_MAX),
        "capped": total > COUNT_MAX,
        "ready": True,
    }

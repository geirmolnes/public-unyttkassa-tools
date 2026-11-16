"""SQLite storage: favourites, recent, collections (per user) and shared ownership data."""

import json
import os
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DB_NAME = "regnskap.db"
RECENT_KEEP = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS favourites (
    email TEXT NOT NULL,
    orgnr TEXT NOT NULL,
    name TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (email, orgnr)
);

CREATE TABLE IF NOT EXISTS recent (
    email TEXT NOT NULL,
    orgnr TEXT NOT NULL,
    name TEXT NOT NULL,
    viewed_at TEXT NOT NULL,
    PRIMARY KEY (email, orgnr)
);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS collections_email ON collections (email);

CREATE TABLE IF NOT EXISTS collection_members (
    collection_id INTEGER NOT NULL REFERENCES collections (id) ON DELETE CASCADE,
    orgnr TEXT NOT NULL,
    name TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (collection_id, orgnr)
);

CREATE TABLE IF NOT EXISTS ownership (
    parent_orgnr TEXT NOT NULL,
    source_year INTEGER NOT NULL,
    child_name TEXT NOT NULL,
    child_orgnr TEXT,
    country TEXT,
    place TEXT,
    share_pct REAL,
    source_page INTEGER,
    extracted_at TEXT NOT NULL,
    model TEXT,
    PRIMARY KEY (parent_orgnr, source_year, child_name)
);
CREATE INDEX IF NOT EXISTS ownership_child ON ownership (child_orgnr);

CREATE TABLE IF NOT EXISTS extractions (
    orgnr TEXT NOT NULL,
    year INTEGER NOT NULL,
    status TEXT NOT NULL,
    n_rows INTEGER,
    error TEXT,
    model TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (orgnr, year)
);

-- Årsregnskap read page by page (src/accounts). The PDF is stored; every value points back to a page of it.
CREATE TABLE IF NOT EXISTS pdf_files (
    sha256 TEXT PRIMARY KEY,
    orgnr TEXT NOT NULL,
    year INTEGER NOT NULL,
    path TEXT NOT NULL,
    n_pages INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pdf_files_doc ON pdf_files (orgnr, year);

-- One model call on one page: variant base (page read), numlist, text, or a repair re-read (fill/align/think).
CREATE TABLE IF NOT EXISTS page_reads (
    sha256 TEXT NOT NULL,
    page INTEGER NOT NULL,
    variant TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    fallback INTEGER NOT NULL DEFAULT 0,
    cost REAL,
    read_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (sha256, page, variant)
);

-- Reading one company-year: progress while running; picked re-reads and remaining failed checks when done.
CREATE TABLE IF NOT EXISTS account_reads (
    orgnr TEXT NOT NULL,
    year INTEGER NOT NULL,
    sha256 TEXT,
    status TEXT NOT NULL,
    phase TEXT,
    pages_total INTEGER,
    pages_done INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    model TEXT,
    choices TEXT,
    failures TEXT,
    n_checks INTEGER,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (orgnr, year)
);

-- What happened (src/events.py): read steps, warnings and errors with tracebacks, errors seen in the browser.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    release TEXT NOT NULL,
    process TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    orgnr TEXT,
    year INTEGER,
    message TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS events_at ON events (at);
CREATE INDEX IF NOT EXISTS events_doc ON events (orgnr, year);

-- Every extracted value, for queries across companies and years. source: statement | note | inline.
CREATE TABLE IF NOT EXISTS figures (
    orgnr TEXT NOT NULL,
    year INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    figures_for TEXT,
    copy TEXT,
    note TEXT,
    label TEXT,
    key TEXT,
    col TEXT,
    value INTEGER NOT NULL,
    unit_scale INTEGER NOT NULL,
    page INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS figures_doc ON figures (orgnr, year);
CREATE INDEX IF NOT EXISTS figures_key ON figures (key);

-- Running text of the filings, searchable (notes, board's and auditor's reports).
CREATE VIRTUAL TABLE IF NOT EXISTS text_fts USING fts5(
    text, orgnr UNINDEXED, year UNINDEXED, page UNINDEXED, note UNINDEXED, kind UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 0'
);

-- What Brreg sent for companies people opened (src/archive.py): /regnskap rows per period, kept after Brreg drops
-- them, and every distinct version of each JSON source (enhet, roller, regnskap:<type>:<period end>).
CREATE TABLE IF NOT EXISTS brreg_regnskap (
    orgnr TEXT NOT NULL,
    kind TEXT NOT NULL,
    to_date TEXT NOT NULL,
    data TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (orgnr, kind, to_date)
);
CREATE TABLE IF NOT EXISTS brreg_snapshots (
    orgnr TEXT NOT NULL,
    source TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    data TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (orgnr, source, sha256)
);

-- Kunngjøringer from w2.brreg.no: one row per announcement, `data` (parsed JSON) once read.
-- Announcements don't change; the list per company is fetched again after KUNNGJORING_TTL.
CREATE TABLE IF NOT EXISTS kunngjoringer (
    kid TEXT PRIMARY KEY,
    orgnr TEXT NOT NULL,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS kunngjoringer_orgnr ON kunngjoringer (orgnr);
CREATE TABLE IF NOT EXISTS kunngjoring_lists (
    orgnr TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL
);

-- Eierkart built from the shareholder register, kept so it opens without being rebuilt.
CREATE TABLE IF NOT EXISTS structures (
    orgnr TEXT NOT NULL,
    year INTEGER NOT NULL,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (orgnr, year)
);
"""


def data_dir() -> Path:
    """REGNSKAP_DATA_DIR, else /app/data (Dokku mount), else ./data."""
    if env := os.environ.get("REGNSKAP_DATA_DIR"):
        return Path(env)
    docker = Path("/app/data")
    return docker if docker.exists() else Path("data")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def connect() -> sqlite3.Connection:
    """Connection with WAL, foreign keys and dict-like rows. Commits on `with` exit."""
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path / DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params))]


def _one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    rows = _all(sql, params)
    return rows[0] if rows else None


def _run(sql: str, params: Iterable[Any] = ()) -> int:
    """Execute a write; returns rowcount."""
    with connect() as conn:
        return conn.execute(sql, tuple(params)).rowcount


# Columns added after the table first shipped: (table, column, type)
ADDED_COLUMNS = [
    ("account_reads", "name", "TEXT"),
    # The process reading it and when it last said so; a read nobody vouches for is taken over (pipeline.watch)
    ("account_reads", "owner", "TEXT"),
    ("account_reads", "heartbeat_at", "TEXT"),
    ("account_reads", "attempts", "INTEGER NOT NULL DEFAULT 0"),
]


def init_db() -> None:
    """Create tables and add missing columns; extractions left running by a restart can never finish, so fail them.
    Unfinished account reads are left for pipeline.watch to resume."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, col, kind in ADDED_COLUMNS:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        conn.execute(
            "UPDATE extractions SET status = 'failed', error = ?, finished_at = ? "
            "WHERE status = 'running'",
            ("Avbrutt av omstart. Prøv igjen.", now_iso()),
        )


# ---------- Favourites ----------


def list_favourites(email: str) -> list[dict[str, Any]]:
    return _all(
        "SELECT orgnr, name FROM favourites WHERE email = ? ORDER BY added_at, rowid", (email,)
    )


def add_favourite(email: str, orgnr: str, name: str) -> None:
    _run(
        "INSERT INTO favourites (email, orgnr, name, added_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (email, orgnr) DO UPDATE SET name = excluded.name",
        (email, orgnr, name, now_iso()),
    )


def remove_favourite(email: str, orgnr: str) -> None:
    _run("DELETE FROM favourites WHERE email = ? AND orgnr = ?", (email, orgnr))


def is_favourite(email: str, orgnr: str) -> bool:
    return (
        _one("SELECT 1 FROM favourites WHERE email = ? AND orgnr = ?", (email, orgnr)) is not None
    )


# ---------- Recent ----------


def touch_recent(email: str, orgnr: str, name: str) -> None:
    """Record a view and keep only the newest RECENT_KEEP per user."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO recent (email, orgnr, name, viewed_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (email, orgnr) DO UPDATE SET name = excluded.name, "
            "viewed_at = excluded.viewed_at",
            (email, orgnr, name, now_iso()),
        )
        conn.execute(
            "DELETE FROM recent WHERE email = ? AND orgnr NOT IN "
            "(SELECT orgnr FROM recent WHERE email = ? ORDER BY viewed_at DESC, rowid DESC LIMIT ?)",
            (email, email, RECENT_KEEP),
        )


def remove_recent(email: str, orgnr: str) -> None:
    _run("DELETE FROM recent WHERE email = ? AND orgnr = ?", (email, orgnr))


def list_recent(email: str, limit: int = 10) -> list[dict[str, Any]]:
    return _all(
        "SELECT orgnr, name FROM recent WHERE email = ? ORDER BY viewed_at DESC, rowid DESC LIMIT ?",
        (email, limit),
    )


# ---------- Collections ----------


def list_collections(email: str) -> list[dict[str, Any]]:
    """Collections with member count and up to three member names."""
    out = []
    for c in _all(
        "SELECT id, name FROM collections WHERE email = ? ORDER BY created_at, id", (email,)
    ):
        members = _all(
            "SELECT name FROM collection_members WHERE collection_id = ? ORDER BY added_at, rowid",
            (c["id"],),
        )
        out.append({**c, "count": len(members), "preview": [m["name"] for m in members[:3]]})
    return out


def create_collection(email: str, name: str, members: list[tuple[str, str]] = ()) -> int:
    """New collection with optional (orgnr, name) members; returns its id."""
    with connect() as conn:
        cid = conn.execute(
            "INSERT INTO collections (email, name, created_at) VALUES (?, ?, ?)",
            (email, name, now_iso()),
        ).lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO collection_members (collection_id, orgnr, name, added_at) "
            "VALUES (?, ?, ?, ?)",
            [(cid, o, n, now_iso()) for o, n in members],
        )
    return cid


def get_collection(email: str, cid: int) -> dict[str, Any] | None:
    """Collection with members, or None if missing or owned by someone else."""
    c = _one("SELECT id, name FROM collections WHERE id = ? AND email = ?", (cid, email))
    if not c:
        return None
    c["members"] = _all(
        "SELECT orgnr, name FROM collection_members WHERE collection_id = ? ORDER BY added_at, rowid",
        (cid,),
    )
    return c


def rename_collection(email: str, cid: int, name: str) -> bool:
    return (
        _run("UPDATE collections SET name = ? WHERE id = ? AND email = ?", (name, cid, email)) > 0
    )


def delete_collection(email: str, cid: int) -> bool:
    return _run("DELETE FROM collections WHERE id = ? AND email = ?", (cid, email)) > 0


def add_member(cid: int, orgnr: str, name: str) -> None:
    _run(
        "INSERT INTO collection_members (collection_id, orgnr, name, added_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (collection_id, orgnr) DO UPDATE SET name = excluded.name",
        (cid, orgnr, name, now_iso()),
    )


def remove_member(cid: int, orgnr: str) -> None:
    _run("DELETE FROM collection_members WHERE collection_id = ? AND orgnr = ?", (cid, orgnr))


def collections_containing(email: str, orgnr: str) -> list[int]:
    rows = _all(
        "SELECT c.id FROM collections c JOIN collection_members m ON m.collection_id = c.id "
        "WHERE c.email = ? AND m.orgnr = ? ORDER BY c.id",
        (email, orgnr),
    )
    return [r["id"] for r in rows]


# ---------- Ownership + extractions (shared across users) ----------


def latest_extraction(orgnr: str) -> dict[str, Any] | None:
    return _one("SELECT * FROM extractions WHERE orgnr = ? ORDER BY year DESC LIMIT 1", (orgnr,))


def get_extraction(orgnr: str, year: int) -> dict[str, Any] | None:
    return _one("SELECT * FROM extractions WHERE orgnr = ? AND year = ?", (orgnr, year))


def start_extraction(orgnr: str, year: int, model: str) -> None:
    _run(
        "INSERT INTO extractions (orgnr, year, status, model, started_at) "
        "VALUES (?, ?, 'running', ?, ?) ON CONFLICT (orgnr, year) DO UPDATE SET "
        "status = 'running', n_rows = NULL, error = NULL, model = excluded.model, "
        "started_at = excluded.started_at, finished_at = NULL",
        (orgnr, year, model, now_iso()),
    )


def finish_extraction(
    orgnr: str, year: int, status: str, n_rows: int | None = None, error: str | None = None
) -> None:
    _run(
        "UPDATE extractions SET status = ?, n_rows = ?, error = ?, finished_at = ? "
        "WHERE orgnr = ? AND year = ?",
        (status, n_rows, error, now_iso(), orgnr, year),
    )


def replace_ownership(parent: str, year: int, rows: list[dict[str, Any]], model: str) -> None:
    """Swap one parent-year's subsidiaries for `rows` (keys: name, orgnr, country, place,
    share_pct, page). Duplicate names keep the first row."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM ownership WHERE parent_orgnr = ? AND source_year = ?", (parent, year)
        )
        conn.executemany(
            "INSERT OR IGNORE INTO ownership (parent_orgnr, source_year, child_name, child_orgnr, "
            "country, place, share_pct, source_page, extracted_at, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    parent,
                    year,
                    r["name"],
                    r.get("orgnr"),
                    r.get("country"),
                    r.get("place"),
                    r.get("share_pct"),
                    r.get("page"),
                    now_iso(),
                    model,
                )
                for r in rows
            ],
        )


def subsidiaries_of(parent: str, year: int) -> list[dict[str, Any]]:
    return _all(
        "SELECT child_name AS name, child_orgnr AS orgnr, country, place, share_pct, "
        "source_page AS page FROM ownership WHERE parent_orgnr = ? AND source_year = ? "
        "ORDER BY rowid",
        (parent, year),
    )


def owned_by(child_orgnr: str) -> list[dict[str, Any]]:
    """Parents listing `child_orgnr`, newest source year per parent."""
    return _all(
        "SELECT o.parent_orgnr AS orgnr, o.share_pct, o.source_year, o.source_page "
        "FROM ownership o WHERE o.child_orgnr = ? AND o.source_year = "
        "(SELECT MAX(source_year) FROM ownership WHERE parent_orgnr = o.parent_orgnr) "
        "ORDER BY o.parent_orgnr",
        (child_orgnr,),
    )


# ---------- Saved structures (eierkart) ----------


def save_structure(orgnr: str, year: int, version: int, data: dict[str, Any]) -> str:
    """Store (or replace) one company-year's structure; returns its created_at."""
    created = now_iso()
    _run(
        "INSERT OR REPLACE INTO structures (orgnr, year, version, data, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (orgnr, year, version, json.dumps(data, ensure_ascii=False), created),
    )
    return created


def get_structure(orgnr: str, year: int, version: int) -> dict[str, Any] | None:
    """The saved structure with `created_at`, or None if missing or built under other rules."""
    row = _one(
        "SELECT data, created_at FROM structures WHERE orgnr = ? AND year = ? AND version = ?",
        (orgnr, year, version),
    )
    return {**json.loads(row["data"]), "created_at": row["created_at"]} if row else None


def structure_saved(orgnr: str, year: int, version: int) -> str | None:
    """created_at of the saved structure, without loading it."""
    row = _one(
        "SELECT created_at FROM structures WHERE orgnr = ? AND year = ? AND version = ?",
        (orgnr, year, version),
    )
    return row["created_at"] if row else None


# ---------- Kunngjøringer ----------

KUNNGJORING_TTL = timedelta(hours=24)


def kunngjoringer_fetched(orgnr: str) -> str | None:
    """When the list for `orgnr` was last fetched (ISO), or None."""
    row = _one("SELECT fetched_at FROM kunngjoring_lists WHERE orgnr = ?", (orgnr,))
    return row["fetched_at"] if row else None


def kunngjoringer_fresh(orgnr: str) -> bool:
    fetched = kunngjoringer_fetched(orgnr)
    return bool(fetched) and datetime.fromisoformat(fetched) > datetime.now(UTC) - KUNNGJORING_TTL


def kunngjoring_unread(orgnr: str, limit: int) -> list[str]:
    """Announcements listed but not read yet, newest first."""
    rows = _all(
        "SELECT kid FROM kunngjoringer WHERE orgnr = ? AND data IS NULL ORDER BY seq LIMIT ?",
        (orgnr, limit),
    )
    return [r["kid"] for r in rows]


def save_kunngjoring_list(orgnr: str, items: list[dict[str, str]]) -> None:
    """Store the list (newest first; `seq` keeps Brreg's order within a day), stamped now."""
    with connect() as conn:
        for seq, it in enumerate(items):
            conn.execute(
                "INSERT INTO kunngjoringer (kid, orgnr, date, type, seq) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (kid) DO UPDATE SET date = excluded.date, type = excluded.type, "
                "seq = excluded.seq",
                (it["kid"], orgnr, it["date"], it["type"], seq),
            )
        conn.execute(
            "INSERT OR REPLACE INTO kunngjoring_lists (orgnr, fetched_at) VALUES (?, ?)",
            (orgnr, now_iso()),
        )


def save_kunngjoring_details(details: dict[str, dict[str, Any]]) -> None:
    with connect() as conn:
        for kid, d in details.items():
            conn.execute(
                "UPDATE kunngjoringer SET data = ? WHERE kid = ?",
                (json.dumps(d, ensure_ascii=False), kid),
            )


def kunngjoring_orgnr(kid: str) -> str | None:
    row = _one("SELECT orgnr FROM kunngjoringer WHERE kid = ?", (kid,))
    return row["orgnr"] if row else None


def list_kunngjoringer(orgnr: str) -> list[dict[str, Any]]:
    """Every stored announcement for `orgnr`, newest first, details merged in."""
    rows = _all(
        "SELECT kid, date, type, data FROM kunngjoringer WHERE orgnr = ? ORDER BY seq", (orgnr,)
    )
    out = []
    for r in rows:
        data = r.pop("data")
        out.append({**json.loads(data or "{}"), **r, "read": data is not None})
    return out

"""Aksjonærregisteret: Skatteetaten's yearly «aksjeeiebok» CSVs → shareholders.db.

Usage: uv run python -m src.shareholders ~/Downloads/aksjeeiebok_20*.csv
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.db import data_dir, now_iso

DB_NAME = "shareholders.db"
LIVE_PATH_ENV = "SHAREHOLDERS_DB_PATH"
HEADER = [
    "Orgnr",
    "Selskap",
    "Aksjeklasse",
    "Navn aksjonær",
    "Fødselsår/orgnr",
    "Postnr/sted",
    "Landkode",
    "Antall aksjer",
    "Antall aksjer selskap",
]
BATCH = 50_000
MAX_SKIPPED_EXAMPLES = 5
FILE_YEAR = re.compile(r"aksjeeiebok_(\d{4})\.csv$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS share_classes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS companies (
    year INTEGER NOT NULL,
    orgnr TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (year, orgnr)
) WITHOUT ROWID;

-- «Antall aksjer selskap» is constant per company and class, so it lives here, not per holding.
CREATE TABLE IF NOT EXISTS company_classes (
    year INTEGER NOT NULL,
    orgnr TEXT NOT NULL,
    class_id INTEGER NOT NULL REFERENCES share_classes (id),
    total_shares INTEGER,  -- NULL where the source has it blank or 0

    PRIMARY KEY (year, orgnr, class_id)
) WITHOUT ROWID;

-- One row per holder per company and class. holder_orgnr for companies, holder_birth_year for
-- people; both NULL for most foreign holders. The source lists some holders several times with
-- identical fields (separate lots that only add up to the company total together); `lots` counts
-- the source lines summed into `shares`.
CREATE TABLE IF NOT EXISTS holdings (
    year INTEGER NOT NULL,
    orgnr TEXT NOT NULL,
    class_id INTEGER NOT NULL REFERENCES share_classes (id),
    holder_name TEXT,
    holder_orgnr TEXT,
    holder_birth_year INTEGER,
    holder_postcode TEXT,
    holder_place TEXT,
    country TEXT,
    shares INTEGER NOT NULL,
    lots INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    year INTEGER PRIMARY KEY,
    file TEXT NOT NULL,
    rows INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
"""

# Built after bulk loads; inserting 17M rows into indexed tables is several times slower.
INDEXES = """
CREATE INDEX IF NOT EXISTS holdings_company ON holdings (orgnr, year);
CREATE INDEX IF NOT EXISTS holdings_holder ON holdings (holder_orgnr, year)
    WHERE holder_orgnr IS NOT NULL;
CREATE INDEX IF NOT EXISTS holdings_person ON holdings (holder_name, holder_birth_year)
    WHERE holder_orgnr IS NULL;
"""
INDEX_NAMES = ("holdings_company", "holdings_holder", "holdings_person")


class BadFile(Exception):
    """Not an aksjeeiebok CSV (unexpected header or file name), or rows not grouped by company."""


@dataclass
class ImportStats:
    year: int
    rows: int = 0
    skipped: int = 0
    skipped_examples: list[tuple[int, str]] = field(default_factory=list)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """shareholders.db in the data dir (or `path`), schema applied."""
    if path is None:
        data_dir().mkdir(parents=True, exist_ok=True)
        path = data_dir() / DB_NAME
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def live_path() -> Path:
    """Where the app reads the register: SHAREHOLDERS_DB_PATH (prod: datachat's copy, mounted
    read-only), else shareholders.db in the data dir."""
    if env := os.environ.get(LIVE_PATH_ENV):
        return Path(env)
    return data_dir() / DB_NAME


def open_live() -> sqlite3.Connection:
    """Read-only connection to the live register; raises FileNotFoundError if it isn't there.

    Datachat adds indexes to its copy, briefly in WAL mode. Like datachat, open immutable (no
    locking, works on a read-only mount) only while the WAL is empty; otherwise a plain read-only
    open, so committed-but-uncheckpointed pages aren't missed."""
    path = live_path()
    if not path.exists():
        raise FileNotFoundError(path)
    wal = path.with_name(path.name + "-wal")
    mode = "mode=ro" if wal.exists() and wal.stat().st_size else "immutable=1"
    conn = sqlite3.connect(f"file:{path}?{mode}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def year_from_path(path: Path) -> int:
    if m := FILE_YEAR.search(path.name):
        return int(m.group(1))
    raise BadFile(f"{path.name}: expected a name like aksjeeiebok_2025.csv")


def parse_holder_id(value: str) -> tuple[str | None, int | None]:
    """«Fødselsår/orgnr» → (orgnr, birth year). Foreign holders have a blank field."""
    v = value.strip()
    if not v.isdigit():
        return None, None
    if len(v) == 9:
        return v, None
    if len(v) == 4:
        return None, int(v)
    return None, None


def _class_id(conn: sqlite3.Connection, cache: dict[str, int], name: str) -> int:
    if name not in cache:
        conn.execute("INSERT OR IGNORE INTO share_classes (name) VALUES (?)", (name,))
        cache[name] = conn.execute(
            "SELECT id FROM share_classes WHERE name = ?", (name,)
        ).fetchone()[0]
    return cache[name]


def clean_text(value: str) -> str | None:
    """Collapse runs of whitespace; blank → None."""
    return " ".join(value.split()) or None


def split_place(value: str, country: str | None) -> tuple[str | None, str | None]:
    """«Postnr/sted» → (postcode, place). Norwegian rows come as «0191 OSLO» or «0191, OSLO»;
    foreign ones are free text («, 1011 PZ; Amsterdam») and stay whole, minus the leading «, »."""
    text = clean_text(value)
    text = text.strip(" ,") if text else None
    if not text:
        return None, None
    if country == "NO" and (m := re.fullmatch(r"(\d{4}),? (.+)", text)):
        return m.group(1), m.group(2)
    return None, text


def import_file(conn: sqlite3.Connection, path: Path) -> ImportStats:
    """Replace one year with the cleaned contents of `path`, in one transaction. Malformed lines
    are skipped and counted rather than aborting a 3M-row load."""
    year = year_from_path(path)
    stats = ImportStats(year)
    classes: dict[str, int] = {}
    companies: dict[str, str] = {}
    totals: dict[tuple[str, int], int | None] = {}
    current: str | None = None
    lots: dict[tuple, list[int]] = {}  # holder key → [shares, lots], for the current company
    batch: list[tuple] = []
    with open(path, encoding="utf-8-sig", newline="") as f, conn:
        # Names contain stray quotes («QWELL HOLDING LTD. ("SELSKAP»); the file has no quoting.
        reader = csv.reader(f, delimiter=";", quoting=csv.QUOTE_NONE)
        if next(reader, None) != HEADER:
            raise BadFile(f"{path.name}: unexpected header")
        for table in ("holdings", "company_classes", "companies"):
            conn.execute(f"DELETE FROM {table} WHERE year = ?", (year,))
        for line_no, row in enumerate(reader, start=2):
            parsed = _parse_row(row)
            if parsed is None:
                stats.skipped += 1
                if len(stats.skipped_examples) < MAX_SKIPPED_EXAMPLES:
                    stats.skipped_examples.append((line_no, ";".join(row)[:200]))
                continue
            orgnr, name, cls, holder, holder_id, place, country, shares, total = parsed
            if orgnr != current:
                # Lots are summed per company, which needs each company's lines to be adjacent.
                if orgnr in companies:
                    raise BadFile(f"{path.name}: line {line_no}: {orgnr} seen earlier in the file")
                _collect(batch, year, current, lots)
                current = orgnr
            class_id = _class_id(conn, classes, cls)
            companies[orgnr] = name
            totals[(orgnr, class_id)] = total or None  # 0 is as unknown as blank
            key = (
                class_id,
                holder,
                *parse_holder_id(holder_id),
                *split_place(place, country),
                country,
            )
            acc = lots.setdefault(key, [0, 0])
            acc[0] += shares
            acc[1] += 1
            stats.rows += 1
            if len(batch) >= BATCH:
                _flush(conn, batch)
        _collect(batch, year, current, lots)
        _flush(conn, batch)
        conn.executemany(
            "INSERT INTO companies (year, orgnr, name) VALUES (?, ?, ?)",
            ((year, o, n) for o, n in companies.items()),
        )
        conn.executemany(
            "INSERT INTO company_classes (year, orgnr, class_id, total_shares) VALUES (?, ?, ?, ?)",
            ((year, o, c, t) for (o, c), t in totals.items()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO imports (year, file, rows, skipped, imported_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (year, path.name, stats.rows, stats.skipped, now_iso()),
        )
    return stats


def _parse_row(row: list[str]) -> tuple | None:
    """Typed, whitespace-cleaned fields, or None for a line that can't be read as one holding."""
    if len(row) > len(HEADER) and re.fullmatch(r"[A-Z]{2}", row[-3].strip()):
        # Only «Postnr/sted» has held a ";" so far («, 1011 PZ; Amsterdam», 2020–2022).
        row = [*row[:5], ";".join(row[5:-3]), *row[-3:]]
    if len(row) != len(HEADER):
        return None
    orgnr, name, cls, holder, holder_id, place, country, shares, total = (c.strip() for c in row)
    if not (orgnr.isdigit() and shares.isdigit() and (total.isdigit() or total == "")):
        return None
    return (
        orgnr,
        clean_text(name) or "",
        clean_text(cls) or "",
        clean_text(holder),
        holder_id,
        place,
        country or None,
        int(shares),
        int(total) if total else None,  # blank for Ernström Electro AS in 2023
    )


def _collect(
    batch: list[tuple], year: int, orgnr: str | None, lots: dict[tuple, list[int]]
) -> None:
    """Move one company's summed holdings into `batch`."""
    if orgnr is not None:
        batch.extend((year, orgnr, *key, shares, n) for key, (shares, n) in lots.items())
    lots.clear()


def _flush(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO holdings (year, orgnr, class_id, holder_name, holder_orgnr, "
        "holder_birth_year, holder_postcode, holder_place, country, shares, lots) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        batch,
    )
    batch.clear()


# Each query counts rows breaking a cleanliness rule; all must be 0 after an import.
INVARIANTS = {
    "untrimmed or double-spaced text": """
        SELECT COUNT(*) FROM holdings WHERE holder_name != trim(holder_name)
           OR holder_name LIKE '%  %' OR holder_place != trim(holder_place)
           OR holder_place LIKE '%  %'""",
    "empty strings instead of NULL": """
        SELECT COUNT(*) FROM holdings WHERE holder_name = '' OR holder_place = ''
           OR holder_postcode = '' OR country = ''""",
    "Norwegian postcode not 4 digits": """
        SELECT COUNT(*) FROM holdings WHERE holder_postcode IS NOT NULL
           AND holder_postcode NOT GLOB '[0-9][0-9][0-9][0-9]'""",
    "duplicate holder rows": """
        SELECT COUNT(*) FROM (SELECT 1 FROM holdings GROUP BY year, orgnr, class_id, holder_name,
           holder_orgnr, holder_birth_year, holder_postcode, holder_place, country
           HAVING COUNT(*) > 1)""",
    "zero company totals": "SELECT COUNT(*) FROM company_classes WHERE total_shares = 0",
    "source lines lost": """
        SELECT COUNT(*) FROM imports i WHERE i.rows !=
           (SELECT COALESCE(SUM(lots), 0) FROM holdings h WHERE h.year = i.year)""",
}


def check(conn: sqlite3.Connection) -> dict[str, int]:
    """Broken invariants → number of offending rows (empty when the data is clean)."""
    counts = {name: conn.execute(sql).fetchone()[0] for name, sql in INVARIANTS.items()}
    return {name: n for name, n in counts.items() if n}


def import_files(conn: sqlite3.Connection, paths: list[Path]) -> list[ImportStats]:
    """Import each file with indexes dropped, then rebuild indexes and planner stats."""
    for name in INDEX_NAMES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    out = [import_file(conn, p) for p in paths]
    conn.executescript(INDEXES)
    conn.execute("ANALYZE")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--db", type=Path, help=f"default: <data dir>/{DB_NAME}")
    args = ap.parse_args(argv)
    paths = sorted(args.files)
    try:
        for p in paths:
            year_from_path(p)
    except BadFile as e:
        print(e, file=sys.stderr)
        return 2
    conn = connect(args.db)
    started = time.monotonic()
    for s in import_files(conn, paths):
        print(f"{s.year}: {s.rows:,} rows, {s.skipped} skipped")
        for line_no, text in s.skipped_examples:
            print(f"  line {line_no}: {text}")
    print(f"done in {time.monotonic() - started:.0f}s → {args.db or data_dir() / DB_NAME}")
    if broken := check(conn):
        for name, n in broken.items():
            print(f"INVARIANT BROKEN: {name}: {n}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

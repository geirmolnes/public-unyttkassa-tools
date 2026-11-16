"""SQLite storage for drafts and parsed sources."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from unytt_parser import ParsedSource, SourceType

DATA_DIR = Path(os.environ.get("KLADD_DATA_DIR", "data"))
DB_PATH = DATA_DIR / "kladd.sqlite"
PDF_DIR = DATA_DIR / "pdfs"
PROFILE_LOG = DATA_DIR / "profiling_log.md"

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article     TEXT NOT NULL,
    model       TEXT NOT NULL,
    user_email  TEXT,
    quotes_ok   INTEGER,
    quotes_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS draft_sources (
    draft_id        INTEGER NOT NULL REFERENCES drafts(id),
    source_position INTEGER NOT NULL,
    source_type     TEXT NOT NULL,
    source          TEXT NOT NULL,
    headline        TEXT,
    byline          TEXT,
    pub_date        TEXT,
    publication     TEXT,
    markdown        TEXT,
    pdf_path        TEXT,
    PRIMARY KEY (draft_id, source_position)
);

CREATE TABLE IF NOT EXISTS profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline        TEXT NOT NULL,
    draft_id        INTEGER REFERENCES drafts(id),
    model           TEXT,
    source_count    INTEGER,
    prompt_chars    INTEGER,
    setup_ms        INTEGER,
    parse_sources_ms INTEGER,
    first_token_ms  INTEGER,
    stream_ms       INTEGER,
    save_draft_ms   INTEGER,
    total_ms        INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Admin-managed standing rules; user_email NULL = applies to every user.
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    user_email  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_draft_sources_source ON draft_sources(source);
"""

# Cached URL parses older than this are re-fetched (news articles get
# updated/corrected; a stale copy must not feed new drafts forever).
SOURCE_CACHE_TTL_HOURS = 24


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing from older databases."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(drafts)").fetchall()}
    if "user_email" not in cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN user_email TEXT")
        conn.execute("UPDATE drafts SET user_email = ?", ("admin@example.org",))
        conn.commit()
    if "quotes_ok" not in cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN quotes_ok INTEGER")
    if "quotes_json" not in cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN quotes_json TEXT")

    profile_cols = {row[1] for row in conn.execute("PRAGMA table_info(profiles)").fetchall()}
    if profile_cols and "draft_id" not in profile_cols:
        conn.execute("ALTER TABLE profiles ADD COLUMN draft_id INTEGER REFERENCES drafts(id)")
        conn.commit()
    # Must run after the column exists (pre-migration DBs lack draft_id, so
    # this index cannot live in _CREATE_TABLES).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_draft_id ON profiles(draft_id)")


# Schema setup/migration runs once per (process, db path); connections after
# that skip the executescript + PRAGMA table_info overhead per call.
_initialized_paths: set[str] = set()


def _connect() -> sqlite3.Connection:
    """Open (and lazily create) the database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    key = str(DB_PATH)
    if key not in _initialized_paths:
        conn.executescript(_CREATE_TABLES)
        _migrate(conn)
        _initialized_paths.add(key)
    return conn


def _save_pdf(draft_id: int, position: int, pdf_bytes: bytes) -> str:
    """Write PDF to disk, return relative path."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    path = PDF_DIR / f"{draft_id}_{position}.pdf"
    path.write_bytes(pdf_bytes)
    return str(path)


def save_draft(
    article: str,
    model: str,
    sources: list[ParsedSource],
    *,
    user_email: str = "api",
    quotes_ok: bool | None = None,
    quotes_json: str | None = None,
) -> int:
    """Persist a draft and its sources. Returns draft ID."""
    conn = _connect()
    try:
        cur = conn.cursor()
        quotes_ok_int = None if quotes_ok is None else int(quotes_ok)
        cur.execute(
            "INSERT INTO drafts (article, model, user_email, quotes_ok, quotes_json) VALUES (?, ?, ?, ?, ?)",
            (article, model, user_email, quotes_ok_int, quotes_json),
        )
        draft_id = cur.lastrowid

        for i, s in enumerate(sources):
            pdf_path = None
            if s.pdf_bytes:
                pdf_path = _save_pdf(draft_id, i, s.pdf_bytes)

            cur.execute(
                """INSERT INTO draft_sources
                       (draft_id, source_position, source_type, source,
                        headline, byline, pub_date, publication, markdown, pdf_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    draft_id, i, s.source_type.value, s.source,
                    s.headline, s.byline, s.publication_date, s.publication,
                    s.markdown, pdf_path,
                ),
            )

        conn.commit()
        return draft_id
    finally:
        conn.close()


def get_cached_source(url: str) -> ParsedSource | None:
    """Return a recently parsed URL source from the DB, or None.

    Entries older than SOURCE_CACHE_TTL_HOURS are ignored so updated/corrected
    news articles get re-fetched.
    """
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT ds.* FROM draft_sources ds
               JOIN drafts d ON d.id = ds.draft_id
               WHERE ds.source = ? AND ds.source_type = 'URL' AND ds.markdown IS NOT NULL
                 AND d.created_at > datetime('now', ?)
               ORDER BY ds.draft_id DESC LIMIT 1""",
            (url, f"-{SOURCE_CACHE_TTL_HOURS} hours"),
        ).fetchone()
        if not row:
            return None
        return ParsedSource(
            source_id=f"cached-{row['draft_id']}-{row['source_position']}",
            source_type=SourceType.URL,
            source=row["source"],
            headline=row["headline"],
            byline=row["byline"],
            publication_date=row["pub_date"],
            publication=row["publication"],
            markdown=row["markdown"],
        )
    finally:
        conn.close()


def _append_profile_log(
    profile_id: int,
    pipeline: str,
    marks: dict[str, int],
    model: str | None,
    source_count: int | None,
    prompt_chars: int | None,
) -> None:
    """Append a row to the markdown profiling log."""
    header = (
        "| # | Time | Model | Sources | Prompt | Parse | TTFT | Stream | Save | Total |\n"
        "|---|------|-------|---------|--------|-------|------|--------|------|-------|\n"
    )
    needs_header = not PROFILE_LOG.exists() or PROFILE_LOG.stat().st_size == 0

    ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M")
    short_model = (model or "-").replace("google-gla:", "")
    prompt_str = f"{prompt_chars / 1000:.1f}k" if prompt_chars is not None else "-"
    total_str = f"{marks['_total'] / 1000:.1f}s" if "_total" in marks else "-"

    def ms(key: str) -> str:
        v = marks.get(key)
        return f"{v}ms" if v is not None else "-"

    row = (
        f"| {profile_id} | {ts} | {short_model} | {source_count or '-'} | {prompt_str}"
        f" | {ms('parse-sources')} | {ms('first-token')} | {ms('stream-complete')}"
        f" | {ms('save-draft')} | {total_str} |\n"
    )

    with open(PROFILE_LOG, "a") as f:
        if needs_header:
            f.write("# Profiling log\n\n")
            f.write(header)
        f.write(row)


def save_profile(
    pipeline: str,
    marks: dict[str, int],
    *,
    draft_id: int | None = None,
    model: str | None = None,
    source_count: int | None = None,
    prompt_chars: int | None = None,
) -> int:
    """Persist a profiling run to DB and markdown log."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO profiles
                   (pipeline, draft_id, model, source_count, prompt_chars,
                    setup_ms, parse_sources_ms, first_token_ms,
                    stream_ms, save_draft_ms, total_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pipeline, draft_id, model, source_count, prompt_chars,
                marks.get("setup"),
                marks.get("parse-sources"),
                marks.get("first-token"),
                marks.get("stream-complete"),
                marks.get("save-draft"),
                marks.get("_total"),
            ),
        )
        conn.commit()
        profile_id = cur.lastrowid
    finally:
        conn.close()

    _append_profile_log(profile_id, pipeline, marks, model, source_count, prompt_chars)
    return profile_id


def get_draft(draft_id: int, *, user_email: str | None = None) -> dict | None:
    """Fetch a single draft by ID, optionally scoped to a user."""
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        if user_email:
            row = conn.execute(
                "SELECT * FROM drafts WHERE id = ? AND user_email = ?",
                (draft_id, user_email),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_draft_sources(draft_id: int) -> list[dict]:
    """Fetch sources for a draft, ordered by position."""
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM draft_sources WHERE draft_id = ? ORDER BY source_position",
            (draft_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_drafts(limit: int = 50, *, user_email: str) -> list[dict]:
    """List recent drafts for a user, newest first."""
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM drafts WHERE user_email = ? ORDER BY id DESC LIMIT ?",
            (user_email, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_profiles(limit: int = 100) -> list[dict]:
    """List recent profiles, newest first."""
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM profiles ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stats() -> dict:
    """Dashboard stats: draft count, avg generation time, and all runs."""
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row

        total_drafts = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]

        avg_row = conn.execute(
            "SELECT AVG(total_ms) AS avg_total_ms FROM profiles"
        ).fetchone()
        avg_total_ms = round(avg_row["avg_total_ms"]) if avg_row["avg_total_ms"] else None

        # Profiles link to drafts via draft_id. Legacy rows (draft_id NULL)
        # fall back to the old created_at match; MAX() collapses the rare
        # same-second collision instead of multiplying rows.
        raw_runs = conn.execute(
            """SELECT d.id, d.model, d.created_at, d.quotes_ok, d.quotes_json,
                      MAX(COALESCE(p.total_ms, pl.total_ms)) AS total_ms,
                      MAX(COALESCE(p.source_count, pl.source_count)) AS source_count,
                      (SELECT GROUP_CONCAT(ds.source, ' | ')
                       FROM draft_sources ds WHERE ds.draft_id = d.id) AS sources
               FROM drafts d
               LEFT JOIN profiles p ON p.draft_id = d.id
               LEFT JOIN profiles pl ON pl.draft_id IS NULL AND pl.created_at = d.created_at
               GROUP BY d.id
               ORDER BY d.created_at DESC"""
        ).fetchall()

        runs = []
        for row in raw_runs:
            r = dict(row)
            r["quotes"] = json.loads(r.pop("quotes_json")) if r.get("quotes_json") else None
            runs.append(r)

        return {"total_drafts": total_drafts, "avg_total_ms": avg_total_ms, "runs": runs}
    finally:
        conn.close()


def add_rule(text: str, *, user_email: str | None = None) -> int:
    """Store a standing rule, scoped to one user or (None) to everyone."""
    text = text.strip()
    if not text:
        raise ValueError("Regelen er tom.")
    scope = user_email.strip().lower() if user_email and user_email.strip() else None
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO rules (text, user_email) VALUES (?, ?)", (text, scope)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def delete_rule(rule_id: int) -> None:
    """Remove a rule by ID (no-op if missing)."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        conn.commit()
    finally:
        conn.close()


def list_rules() -> list[dict]:
    """All rules, global first, then grouped by user, oldest first."""
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM rules ORDER BY user_email IS NOT NULL, user_email, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def rules_for_user(user_email: str | None) -> list[str]:
    """Rule texts that apply to a user: global ones, then their own."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT text FROM rules
               WHERE user_email IS NULL OR user_email = lower(?)
               ORDER BY user_email IS NOT NULL, id""",
            (user_email or "",),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def known_users() -> list[str]:
    """Emails that have made drafts in this app, for the rule-scope picker."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT DISTINCT lower(user_email) FROM drafts
               WHERE user_email LIKE '%@%' ORDER BY 1"""
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()

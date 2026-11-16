"""SQLite storage for ingested articles and their verdicts."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config, dagbladet

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    feed_name TEXT,
    title TEXT,
    link TEXT,
    summary TEXT,
    body_excerpt TEXT,
    published_at TEXT,
    published_day TEXT,
    image_url TEXT,
    paywalled INTEGER,
    scraper_cursor TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_day DESC, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_link ON articles(source, link);

CREATE TABLE IF NOT EXISTS verdicts (
    article_id TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    prefilter_passed INTEGER NOT NULL,
    prefilter_reason TEXT,
    prefilter_terms TEXT,
    relevant INTEGER,
    score INTEGER,
    stage TEXT,
    kind TEXT,
    topics TEXT,
    rationale TEXT,
    model TEXT,
    prompt_version TEXT,
    classified_at TEXT,
    error TEXT,
    cluster_id TEXT REFERENCES clusters(id)
);

CREATE INDEX IF NOT EXISTS idx_verdicts_score ON verdicts(relevant, score DESC);
CREATE INDEX IF NOT EXISTS idx_verdicts_pending
    ON verdicts(prefilter_passed) WHERE classified_at IS NULL;

CREATE TABLE IF NOT EXISTS clusters (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    first_day TEXT NOT NULL,
    last_day TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clusters_last_day ON clusters(last_day DESC);

-- Whether Utdanningsnytt has already written about the article. Our own article
-- is copied in rather than referenced: the archive lives behind a mount that the
-- web process must never need, so a missing mount costs the badge, not the page.
CREATE TABLE IF NOT EXISTS coverage (
    article_id TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    status TEXT NOT NULL,          -- 'sitert' | 'dekket' | 'oppfolging' | 'ingen'
    method TEXT,                   -- 'lenke' | 'modell'
    internal_id INTEGER,
    internal_url TEXT,
    internal_title TEXT,
    internal_day TEXT,
    rationale TEXT,
    whats_new TEXT,                -- what this article adds that ours does not
    model TEXT,
    prompt_version TEXT,
    checks INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_coverage_status ON coverage(status);

CREATE TABLE IF NOT EXISTS feedback (
    article_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    vote INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (article_id, user_id)
);

-- «Jeg tar denne». The desk's most expensive mistake is two journalists writing
-- the same sitatsak off the same radar hit, and nothing in here could say so.
-- Separate from `feedback` because it answers a different question and one
-- person may both vote on a story and claim it.
CREATE TABLE IF NOT EXISTS claims (
    article_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_email TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (article_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_claims_article ON claims(article_id);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tick_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ingested INTEGER,
    prefiltered_in INTEGER,
    classified INTEGER,
    relevant INTEGER,
    clustered INTEGER,
    new_stories INTEGER,
    joined INTEGER,
    errors INTEGER,
    error TEXT
);
"""

# Full-text index over the mirrored article text. External content: the rows stay
# in `articles` and this stores only the term index, so the 4000-char bodies are
# not duplicated on the volume.
#
# The column NAMES are load-bearing. FTS5 reads an external-content table by
# issuing `SELECT title, summary, body_excerpt FROM articles WHERE rowid = ?`
# using *these* names, so renaming one here leaves MATCH working and makes every
# snippet()/highlight() call raise "SQL logic error" — a failure that first
# appears the day someone searches in production.
#
# `remove_diacritics 0`, and not for the obvious reason. Æ and Ø are atomic
# Unicode letters with no canonical decomposition, so unicode61 never folds them
# at any setting. Å *does* decompose, so at 1 or 2 the tokenizer folds å→a and
# nothing else — «Ås» collides with «as», «måte» with «mate», while «søk»/«sok»
# stays apart. One of three Norwegian letters folded is a rule no reader can
# predict; 0 keeps all three distinct.
#
# `prefix='2 3 4'` because Nordic compounding makes «barnehagelærer» one token:
# «barnehagelærer» does not match the stored «Barnehagelærere», «barnehagelærer*»
# does. The prefix index turns short prefix probes into a b-tree lookup.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title,
    summary,
    body_excerpt,
    content = 'articles',
    content_rowid = 'rowid',
    tokenize = "unicode61 remove_diacritics 0",
    prefix = '2 3 4'
);

CREATE TRIGGER IF NOT EXISTS articles_fts_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, summary, body_excerpt)
    VALUES (new.rowid, new.title, new.summary, new.body_excerpt);
END;

-- The 'delete' row must carry the OLD values verbatim: FTS5 uses them to work
-- out which postings to remove. Re-inserting without deleting first leaves the
-- old terms in the index forever, and the article keeps matching words it no
-- longer contains.
CREATE TRIGGER IF NOT EXISTS articles_fts_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, body_excerpt)
    VALUES ('delete', old.rowid, old.title, old.summary, old.body_excerpt);
END;

CREATE TRIGGER IF NOT EXISTS articles_fts_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, body_excerpt)
    VALUES ('delete', old.rowid, old.title, old.summary, old.body_excerpt);
    INSERT INTO articles_fts(rowid, title, summary, body_excerpt)
    VALUES (new.rowid, new.title, new.summary, new.body_excerpt);
END;
"""

_FTS_DROP = """
DROP TRIGGER IF EXISTS articles_fts_ai;
DROP TRIGGER IF EXISTS articles_fts_ad;
DROP TRIGGER IF EXISTS articles_fts_au;
DROP TABLE IF EXISTS articles_fts;
"""

# Bump to throw the index away and rebuild it. Anything that changes how text is
# tokenised — the tokenizer string, the prefix list, which columns are indexed —
# needs a bump: an FTS5 table cannot be altered in place, and an index half-built
# under one tokenizer and half under another returns wrong answers, not errors.
FTS_VERSION = "1"
FTS_VERSION_KEY = "fts_version"


def now() -> str:
    return datetime.now(UTC).isoformat()


# Columns added after the first deploy. CREATE TABLE IF NOT EXISTS does nothing
# to a table that already exists, so these are applied on every connect.
# (No REFERENCES here — SQLite rejects a foreign key on ALTER TABLE ADD COLUMN
# unless the default is NULL, and the constraint buys nothing we enforce.)
_ADDED_COLUMNS = [
    ("verdicts", "kind", "TEXT"),
    ("verdicts", "cluster_id", "TEXT"),
    ("tick_runs", "clustered", "INTEGER"),
    ("tick_runs", "new_stories", "INTEGER"),
    ("tick_runs", "joined", "INTEGER"),
    ("tick_runs", "cited", "INTEGER"),
    ("tick_runs", "covered", "INTEGER"),
    ("coverage", "whats_new", "TEXT"),
    # What the tick cost, and whether it could answer «har vi den?» at all. A
    # tick that ran without the archive mounted reports cited=0 covered=0, which
    # is byte-identical to one that looked and found nothing.
    ("tick_runs", "llm_calls", "INTEGER"),
    ("tick_runs", "llm_tokens", "INTEGER"),
    ("tick_runs", "archive_seen", "INTEGER"),
]

# Indexes over migrated columns, created once the ALTERs above have run.
_POST_MIGRATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_verdicts_cluster ON verdicts(cluster_id)",
    # Serves both `kilde:` equality and the GROUP BY in source_counts.
    "CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source)",
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    for sql in _POST_MIGRATE_INDEXES:
        conn.execute(sql)
    conn.commit()


def _ensure_fts(conn: sqlite3.Connection) -> None:
    """Create the search index and fill it from the rows already here, once.

    The triggers keep it current from then on, so this runs on the first connect
    after a deploy and never again — which is what the state marker is for. A
    row count would not do: an empty index and an empty `articles` are the same
    number, and the check has to be O(1).

    Every step is idempotent, so two processes racing here waste work rather
    than corrupting anything; the marker is written last, by whichever finishes.
    """
    if get_state(conn, FTS_VERSION_KEY) == FTS_VERSION:
        return
    conn.executescript(_FTS_DROP)
    conn.executescript(FTS_SCHEMA)
    # 'rebuild' discards the index and re-reads every row of `articles` through
    # the triggers' own column mapping — one statement, and also the repair if
    # the index ever drifts. 'optimize' merges the posting lists; it rewrites
    # the whole index, so it belongs here and never in the tick.
    conn.execute("INSERT INTO articles_fts(articles_fts) VALUES ('rebuild')")
    conn.execute("INSERT INTO articles_fts(articles_fts) VALUES ('optimize')")
    conn.commit()
    set_state(conn, FTS_VERSION_KEY, FTS_VERSION)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the app DB, creating parent dirs and schema if needed."""
    db_path = db_path or config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # The cron tick writes while the web process reads. Without a busy timeout
    # SQLite raises SQLITE_BUSY on the first contended lock instead of waiting,
    # so a page load landing mid-tick 500s before it runs a single query.
    conn.execute("PRAGMA busy_timeout = 5000")
    # WAL is safe for *this* database. The README's warning against it concerns
    # the mounted, read-only, foreign-uid copies (news_scraper.db,
    # unytt_artikler.db) where a :ro mount cannot write the -shm file.
    # utdanningsradar.db lives on the app's own read-write volume under the
    # app's own uid, so the objection does not apply — and WAL is what stops a
    # reader blocking behind the tick's write at all.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Declared ON DELETE CASCADE does nothing without this; SQLite defaults it
    # off per connection.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    _ensure_fts(conn)
    return conn


# Older than this an article is not news and not reachable through any view the
# app offers — the feed's own sparkline only reaches back 21 days.
PRUNE_AFTER_DAYS = 90


def prune(conn: sqlite3.Connection, *, days: int = PRUNE_AFTER_DAYS) -> int:
    """Drop articles too old to show. Returns how many went.

    Proven citations are kept whatever their age: «sitert av oss» is the one
    answer in here that took real reporting to establish, and it costs a row.
    Verdicts and coverage follow through ON DELETE CASCADE, which is why
    connect() turns foreign keys on.
    """
    cutoff = (datetime.now(config.OSLO).date() - timedelta(days=days)).isoformat()
    n = conn.execute(
        """
        DELETE FROM articles
         WHERE published_day < ?
           AND id NOT IN (SELECT article_id FROM coverage WHERE status = 'sitert')
        """,
        (cutoff,),
    ).rowcount
    # Never articles, whatever their age — see dagbladet.is_studio_post and
    # urls.is_page_anchor. Ingest refuses them now; this clears what got in
    # before it did, and anything a future scraper change lets through again.
    n += conn.execute(
        "DELETE FROM articles WHERE source = ? AND link LIKE ?",
        (dagbladet.SOURCE, dagbladet.STUDIO_LIKE),
    ).rowcount
    n += conn.execute(
        "DELETE FROM articles WHERE link LIKE '%?pinnedEntry=%' OR link LIKE '%&pinnedEntry=%'"
    ).rowcount
    # One row per link. Ingest folds a re-minted id into the row it already has;
    # this collapses the pairs stored before it did, keeping the newest copy
    # and, as always, anything we have proven we cited.
    n += conn.execute(
        """
        DELETE FROM articles
         WHERE id IN (
            SELECT a.id
              FROM articles a
              JOIN articles b ON b.source = a.source AND b.link = a.link AND b.id != a.id
             WHERE a.link IS NOT NULL
               AND (b.ingested_at > a.ingested_at
                    OR (b.ingested_at = a.ingested_at AND b.id > a.id)))
           AND id NOT IN (SELECT article_id FROM coverage WHERE status = 'sitert')
        """
    ).rowcount
    conn.commit()
    if n:
        # Empty clusters left behind by the cascade would keep being offered as
        # candidates and would keep inflating «løpende saker».
        conn.execute(
            "DELETE FROM clusters "
            "WHERE id NOT IN (SELECT cluster_id FROM verdicts WHERE cluster_id IS NOT NULL)"
        )
        conn.commit()
        conn.execute("VACUUM")
    return n


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def upsert_article(conn: sqlite3.Connection, article: dict) -> None:
    """Store an article."""
    conn.execute(
        """
        INSERT INTO articles (
            id, source, feed_name, title, link, summary, body_excerpt,
            published_at, published_day, image_url, paywalled, scraper_cursor, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            link = excluded.link,
            summary = excluded.summary,
            body_excerpt = excluded.body_excerpt,
            published_at = excluded.published_at,
            published_day = excluded.published_day,
            image_url = excluded.image_url,
            paywalled = excluded.paywalled,
            scraper_cursor = excluded.scraper_cursor
        """,
        (
            article["id"], article["source"], article["feed_name"], article["title"],
            article["link"], article["summary"], article["body_excerpt"],
            article["published_at"], article["published_day"], article["image_url"],
            article["paywalled"], article["scraper_cursor"], now(),
        ),
    )


def article_id_for_link(conn: sqlite3.Connection, source: str, link: str | None) -> str | None:
    """The id already stored for this exact link at this source, if any.

    Exact, not normalised: the duplicates this exists for — a front page that
    mints a new id each time it rewrites the teaser — carry the link character
    for character.
    """
    if not link:
        return None
    row = conn.execute(
        "SELECT id FROM articles WHERE source = ? AND link = ? ORDER BY ingested_at, id LIMIT 1",
        (source, link),
    ).fetchone()
    return row[0] if row else None


def record_prefilter(conn: sqlite3.Connection, article_id: str, result) -> None:
    """Store the lexical verdict, clearing any previous classification.

    An article whose text changed is re-gated and re-classified, so the old
    score must not linger next to new text. The same goes for what we concluded
    about our own coverage of it.
    """
    conn.execute("DELETE FROM coverage WHERE article_id = ?", (article_id,))
    conn.execute(
        """
        INSERT INTO verdicts (article_id, prefilter_passed, prefilter_reason, prefilter_terms)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(article_id) DO UPDATE SET
            prefilter_passed = excluded.prefilter_passed,
            prefilter_reason = excluded.prefilter_reason,
            prefilter_terms = excluded.prefilter_terms,
            relevant = NULL, score = NULL, stage = NULL, kind = NULL, topics = NULL,
            rationale = NULL, classified_at = NULL, error = NULL, cluster_id = NULL
        """,
        (article_id, 1 if result.passed else 0, result.reason, json.dumps(result.terms)),
    )


def pending_classification(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Unclassified articles, excluding explicit model content blocks.

    Blocks stay unclassified and retain their reason for operators. Re-ingest
    or an explicit reclassification clears the error and allows another try.
    """
    sql = """
        SELECT a.id, a.source, a.title, a.summary, a.body_excerpt, a.published_at
          FROM articles a
          JOIN verdicts v ON v.article_id = a.id
         WHERE v.prefilter_passed = 1 AND v.classified_at IS NULL
           AND (v.error IS NULL OR v.error NOT LIKE 'blocked response:%')
         ORDER BY a.published_at DESC
    """
    params: list = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def count_pending_classification(conn: sqlite3.Connection) -> int:
    """How many articles passed the lexical gate and still have no verdict."""
    return conn.execute(
        "SELECT COUNT(*) FROM verdicts WHERE prefilter_passed = 1 AND classified_at IS NULL "
        "AND (error IS NULL OR error NOT LIKE 'blocked response:%')"
    ).fetchone()[0]


def record_classification(
    conn: sqlite3.Connection,
    article_id: str,
    *,
    relevant: bool | None,
    score: int | None,
    stage: str | None,
    kind: str | None,
    topics: list[str] | None,
    rationale: str | None,
    model: str,
    prompt_version: str,
    error: str | None = None,
) -> None:
    """Store an LLM verdict. A failed call leaves classified_at NULL so it retries."""
    conn.execute(
        """
        UPDATE verdicts
           SET relevant = ?, score = ?, stage = ?, kind = ?, topics = ?, rationale = ?,
               model = ?, prompt_version = ?, classified_at = ?, error = ?
         WHERE article_id = ?
        """,
        (
            None if relevant is None else int(relevant),
            score, stage, kind,
            json.dumps(topics) if topics is not None else None,
            rationale, model, prompt_version,
            None if error else now(),
            error, article_id,
        ),
    )
    # Re-scoring can demote an article the last prompt found relevant; it must not
    # keep sitting in a story. A failed call says nothing, so it changes nothing.
    if error is None and not relevant:
        conn.execute(
            "UPDATE verdicts SET cluster_id = NULL WHERE article_id = ?", (article_id,)
        )


def pending_clustering(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Relevant articles not yet tied to a running story, oldest first.

    Oldest first on purpose: a story should be founded by the article that broke
    it, so the ones that follow have something to attach to. `limit` of 0 or
    None means the whole queue. Topics come back parsed — they are what ranks
    the candidate stories.
    """
    sql = """
        SELECT a.id, a.source, a.title, a.summary, a.body_excerpt, a.published_day,
               v.topics
          FROM articles a
          JOIN verdicts v ON v.article_id = a.id
         WHERE v.relevant = 1 AND v.cluster_id IS NULL
         ORDER BY a.published_day, a.published_at
    """
    params: list = []
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [
        {**dict(r), "topics": json.loads(r["topics"]) if r["topics"] else []}
        for r in conn.execute(sql, params)
    ]


def count_pending_clustering(conn: sqlite3.Connection) -> int:
    """How many relevant articles are still waiting for a story."""
    return conn.execute(
        "SELECT COUNT(*) FROM verdicts WHERE relevant = 1 AND cluster_id IS NULL"
    ).fetchone()[0]


def _cluster_members(conn: sqlite3.Connection, cutoff: str) -> dict[str, list[sqlite3.Row]]:
    """Articles of every open cluster, keyed by cluster, oldest first.

    One query for all of them rather than one per cluster: the candidate list is
    rebuilt for every article being placed, so this runs often.
    """
    rows = conn.execute(
        """
        SELECT v.cluster_id, a.title, v.topics
          FROM articles a
          JOIN verdicts v ON v.article_id = a.id
          JOIN clusters c ON c.id = v.cluster_id
         WHERE c.last_day >= ?
         ORDER BY v.cluster_id, a.published_day, a.published_at
        """,
        (cutoff,),
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["cluster_id"], []).append(row)
    return grouped


def open_clusters(conn: sqlite3.Connection, *, days: int) -> list[dict]:
    """Every story still in play, freshest first, with its topics and titles.

    A story is "open" while it has had an article inside the window; nothing is
    ever closed explicitly, it just stops being offered as a candidate.

    Deliberately uncapped: `cluster.shortlist` decides which of these reach the
    prompt. Cutting the list here by recency would drop the story that went
    quiet for three weeks — exactly the one ranking exists to rescue.
    """
    cutoff = (datetime.now(config.OSLO).date() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT c.id, c.label, c.first_day, c.last_day, COUNT(v.article_id) AS n_articles
          FROM clusters c
          JOIN verdicts v ON v.cluster_id = c.id
         WHERE c.last_day >= ?
         GROUP BY c.id
         ORDER BY c.last_day DESC
        """,
        (cutoff,),
    ).fetchall()
    if not rows:
        return []

    members = _cluster_members(conn, cutoff)
    candidates = []
    for row in rows:
        articles = members.get(row["id"], [])
        topics: list[str] = []
        for a in articles:
            topics.extend(json.loads(a["topics"]) if a["topics"] else [])
        candidates.append(
            {
                "id": row["id"],
                "label": row["label"],
                "first_day": row["first_day"],
                "last_day": row["last_day"],
                "n_articles": row["n_articles"],
                "titles": _story_titles(articles),
                "topics": sorted(set(topics)),
            }
        )
    return candidates


# How much of a story the model is shown alongside its name. The founding title
# says what the story is and the latest ones say where it has got to — but the
# reason for showing more than the two edges is drift: a story that has spread
# beyond the event it was founded on only looks wrong when several of its titles
# are on the page at once, and its name is exactly what stops looking wrong.
_CANDIDATE_TITLES = 5


def _story_titles(articles: list[sqlite3.Row]) -> list[str]:
    """The founding title and the most recent ones, oldest first, without repeats."""
    titles = [a["title"] for a in articles if a["title"]]
    if len(titles) > _CANDIDATE_TITLES:
        titles = [titles[0], *titles[-(_CANDIDATE_TITLES - 1) :]]
    return list(dict.fromkeys(titles))


def _coverage_window(days: int) -> str:
    """Oldest Norwegian day still asked about."""
    return (datetime.now(config.OSLO).date() - timedelta(days=days)).isoformat()


def pending_coverage_links(conn: sqlite3.Connection, *, days: int) -> list[sqlite3.Row]:
    """Relevant articles in the window that we have not yet proved we cited.

    Deliberately includes the ones the model already turned down: a sitatsak is
    usually written *after* the radar first sees the source article, so an
    article that looked uncovered this morning can be proven covered tonight.
    Re-asking costs one dict lookup.
    """
    return list(
        conn.execute(
            """
            SELECT a.id, a.link
              FROM articles a
              JOIN verdicts v ON v.article_id = a.id
              LEFT JOIN coverage c ON c.article_id = a.id
             WHERE v.relevant = 1
               AND a.published_day >= ?
               AND a.link IS NOT NULL
               AND COALESCE(c.status, '') != 'sitert'
            """,
            (_coverage_window(days),),
        )
    )


def pending_coverage_check(
    conn: sqlite3.Connection,
    *,
    days: int,
    max_checks: int,
    recheck_hours: int,
    limit: int | None = None,
) -> list[dict]:
    """Relevant articles still worth asking the model about, freshest first.

    An article is asked at most `max_checks` times, and the second time only
    after `recheck_hours` — long enough for us to have published a sitatsak and
    for the archive to have picked it up. A failed call leaves `checked_at` NULL
    and does not count, so it comes back on the next tick.
    """
    stale = (datetime.now(UTC) - timedelta(hours=recheck_hours)).isoformat()
    sql = """
        SELECT a.id, a.source, a.title, a.summary, a.body_excerpt, a.published_day,
               v.topics
          FROM articles a
          JOIN verdicts v ON v.article_id = a.id
          LEFT JOIN coverage c ON c.article_id = a.id
         WHERE v.relevant = 1
           AND a.published_day >= ?
           AND (c.article_id IS NULL
                OR (c.status = 'ingen'
                    AND c.checks < ?
                    AND (c.checked_at IS NULL OR c.checked_at < ?)))
         ORDER BY a.published_day DESC, a.published_at DESC
    """
    params: list = [_coverage_window(days), max_checks, stale]
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [
        {**dict(r), "topics": json.loads(r["topics"]) if r["topics"] else []}
        for r in conn.execute(sql, params)
    ]


def record_coverage(
    conn: sqlite3.Connection,
    article_id: str,
    *,
    status: str,
    method: str | None = None,
    ours: dict | None = None,
    rationale: str | None = None,
    whats_new: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    error: str | None = None,
    asked: bool = False,
) -> None:
    """Store what we know about our own coverage of one article.

    `asked` records that the model was actually consulted, which is what the
    two-attempt budget counts — the free link check may run on every tick
    forever without using one up.
    """
    ours = ours or {}
    conn.execute(
        """
        INSERT INTO coverage (
            article_id, status, method, internal_id, internal_url, internal_title,
            internal_day, rationale, whats_new, model, prompt_version, checks,
            checked_at, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(article_id) DO UPDATE SET
            status = excluded.status,
            method = excluded.method,
            internal_id = excluded.internal_id,
            internal_url = excluded.internal_url,
            internal_title = excluded.internal_title,
            internal_day = excluded.internal_day,
            rationale = excluded.rationale,
            whats_new = excluded.whats_new,
            model = excluded.model,
            prompt_version = excluded.prompt_version,
            checks = coverage.checks + excluded.checks,
            checked_at = excluded.checked_at,
            error = excluded.error
        """,
        (
            article_id, status, method, ours.get("id"), ours.get("url"),
            ours.get("title"), ours.get("day"), rationale, whats_new, model,
            prompt_version, int(asked), None if error else now(), error,
        ),
    )


def create_cluster(conn: sqlite3.Connection, label: str, day: str) -> str:
    """Start a running story on `day`. Returns its id."""
    cluster_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO clusters (id, label, first_day, last_day, created_at) VALUES (?, ?, ?, ?, ?)",
        (cluster_id, label, day, day, now()),
    )
    return cluster_id


def attach_to_cluster(
    conn: sqlite3.Connection, article_id: str, cluster_id: str, day: str
) -> None:
    """Tie an article to a story and widen the story's span to cover it."""
    conn.execute("UPDATE verdicts SET cluster_id = ? WHERE article_id = ?", (cluster_id, article_id))
    conn.execute(
        """
        UPDATE clusters
           SET first_day = MIN(first_day, ?), last_day = MAX(last_day, ?)
         WHERE id = ?
        """,
        (day, day, cluster_id),
    )


def clear_cluster_assignments(
    conn: sqlite3.Connection, *, since: str | None = None
) -> tuple[int, int]:
    """Forget which story each article belongs to. Returns (articles, stories dropped).

    Clustering is cumulative — every article is placed against the stories that
    existed the moment it arrived — so a story that grew wrong keeps attracting
    and no prompt change reaches it. Only forgetting the assignments does.

    A story left with no articles is deleted rather than kept empty, which is
    also how a bad label stops being a candidate.
    """
    where, params = "", []
    if since:
        where = " AND article_id IN (SELECT id FROM articles WHERE published_at >= ?)"
        params.append(since)

    cleared = conn.execute(
        f"UPDATE verdicts SET cluster_id = NULL WHERE cluster_id IS NOT NULL{where}",
        params,
    ).rowcount
    dropped = conn.execute(
        """
        DELETE FROM clusters
         WHERE id NOT IN (SELECT cluster_id FROM verdicts WHERE cluster_id IS NOT NULL)
        """
    ).rowcount
    conn.commit()
    return cleared, dropped


def record_feedback(conn: sqlite3.Connection, article_id: str, user_id: str, vote: int) -> None:
    """Store a thumb. Voting the same way twice clears it, so it toggles."""
    existing = conn.execute(
        "SELECT vote FROM feedback WHERE article_id = ? AND user_id = ?", (article_id, user_id)
    ).fetchone()
    if existing and existing["vote"] == vote:
        conn.execute(
            "DELETE FROM feedback WHERE article_id = ? AND user_id = ?", (article_id, user_id)
        )
    else:
        conn.execute(
            "INSERT INTO feedback (article_id, user_id, vote, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(article_id, user_id) DO UPDATE SET vote = excluded.vote, "
            "created_at = excluded.created_at",
            (article_id, user_id, vote, now()),
        )
    conn.commit()


def vote_for(conn: sqlite3.Connection, article_id: str, user_id: str) -> int | None:
    row = conn.execute(
        "SELECT vote FROM feedback WHERE article_id = ? AND user_id = ?", (article_id, user_id)
    ).fetchone()
    return row["vote"] if row else None


def record_claim(conn: sqlite3.Connection, article_id: str, user_id: str, email: str) -> None:
    conn.execute(
        "INSERT INTO claims (article_id, user_id, user_email, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(article_id, user_id) DO NOTHING",
        (article_id, user_id, email, now()),
    )
    conn.commit()


def release_claim(conn: sqlite3.Connection, article_id: str, user_id: str) -> None:
    conn.execute(
        "DELETE FROM claims WHERE article_id = ? AND user_id = ?", (article_id, user_id)
    )
    conn.commit()


def claims_for(conn: sqlite3.Connection, article_ids: list[str]) -> dict[str, list[dict]]:
    """Who has taken each of these, so the feed can say so on the row."""
    if not article_ids:
        return {}
    holes = ",".join("?" * len(article_ids))
    out: dict[str, list[dict]] = {}
    for row in conn.execute(
        f"SELECT article_id, user_id, user_email, created_at FROM claims "
        f"WHERE article_id IN ({holes}) ORDER BY created_at",
        article_ids,
    ):
        out.setdefault(row["article_id"], []).append(dict(row))
    return out


TICK_LOCK_KEY = "tick_lock"

# A tick that has held the lock this long is not running any more — the container
# was killed, or the process was OOMed before its `finally` ran. Longer than any
# real tick (the caps bound one at a few minutes; measured runs finish in seconds)
# and shorter than two turns of the half-hourly cron, so at most one scheduled run
# is ever skipped by a stale lock. Sized against the cron: at four hours, which is
# what this was when the cron ran every two hours, a stale lock now costs eight.
TICK_LOCK_STALE_HOURS = 1


def acquire_tick_lock(conn: sqlite3.Connection) -> bool:
    """Claim the right to run a tick. False when another one already holds it.

    In the database rather than a lock file: `flock` is not in the slim image,
    and the state table is the thing both a cron run and a manual `dokku run`
    can see. The INSERT is the test — a UNIQUE primary key makes the claim
    atomic, so two ticks starting in the same second cannot both win.
    """
    stale = (datetime.now(UTC) - timedelta(hours=TICK_LOCK_STALE_HOURS)).isoformat()
    conn.execute("DELETE FROM state WHERE key = ? AND value < ?", (TICK_LOCK_KEY, stale))
    cur = conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
        (TICK_LOCK_KEY, now()),
    )
    conn.commit()
    return cur.rowcount > 0


def release_tick_lock(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM state WHERE key = ?", (TICK_LOCK_KEY,))
    conn.commit()


def log_tick_start(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO tick_runs (started_at) VALUES (?)", (now(),))
    conn.commit()
    return cur.lastrowid


def log_tick_finish(conn: sqlite3.Connection, run_id: int, **counts) -> None:
    conn.execute(
        """
        UPDATE tick_runs
           SET finished_at = ?, ingested = ?, prefiltered_in = ?, classified = ?,
               relevant = ?, clustered = ?, new_stories = ?, joined = ?,
               cited = ?, covered = ?, llm_calls = ?, llm_tokens = ?,
               archive_seen = ?, errors = ?, error = ?
         WHERE id = ?
        """,
        (
            now(), counts.get("ingested"), counts.get("prefiltered_in"),
            counts.get("classified"), counts.get("relevant"), counts.get("clustered"),
            counts.get("new_stories"), counts.get("joined"),
            counts.get("cited"), counts.get("covered"),
            counts.get("llm_calls"), counts.get("llm_tokens"),
            counts.get("archive_seen"),
            counts.get("errors"), counts.get("error"), run_id,
        ),
    )
    conn.commit()

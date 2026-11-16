"""Read queries behind the feed."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from itertools import groupby

from .. import config, storage
from .searchq import Query, mute_match

# Display only — the filter values stay as the scraper stores them.
SOURCE_LABELS = {
    "Forskning": "Forskning.no",
    "Barnehage": "Barnehage.no",
}

PER_PAGE = 60

# Whether Utdanningsnytt has the story — strictly what the coverage step
# concluded about *this* article.
#
# Proof on a sibling in the same running story used to count too, and it was
# wrong every time it fired. The two steps have opposite doubt rules on purpose:
# clustering joins when unsure, because a forløp should read as one thread;
# coverage refuses when unsure, because a false «vi har den» costs a sak we
# should have written. Reading one through the other inverts the strict rule —
# in practice every article the roll-up marked was one the coverage model had
# already answered INGEN for, all of them dragged in by a cluster labelled after
# a theme rather than an event.
#
# Written as a correlated EXISTS rather than a join so it can be dropped into
# every query that already has `a` in scope, including the count helpers,
# without any of them growing a join they otherwise don't need.
COVERED = """(
    EXISTS (SELECT 1 FROM coverage c1
             WHERE c1.article_id = a.id AND c1.status IN ('sitert', 'dekket', 'oppfolging'))
)"""

_COVERAGE_SELECT = """
               cov.status AS coverage_status, cov.method AS coverage_method,
               cov.internal_url, cov.internal_title, cov.internal_day,
               cov.rationale AS coverage_rationale, cov.whats_new AS coverage_whats_new
"""

_COVERAGE_JOIN = "LEFT JOIN coverage cov ON cov.article_id = a.id"

# Text relevance, and how far the editorial score is allowed to move it.
#
# bm25() is negative and gets more negative the better the hit, so ASC is
# best-first. Multiplying by (1 + score/100) lets the score scale a hit between
# 1× and 2×; it can never lift a document with no text hit at all, because the
# document has to MATCH to be in the result set in the first place. A weak hit
# at score 100 (-0.5 × 2.0 = -1.0) still loses to a strong hit at score 40
# (-2.0 × 1.4 = -2.8) — text decides who is in the running, the score decides
# the order among near-ties.
#
# Not additive: bm25 magnitudes depend on corpus size and term rarity, so any
# `bm25 + k*score` needs a k that is wrong again next month. A product is
# scale-invariant, and inside one query every row shares the same query terms
# and therefore the same idf, so the comparison is honest.
_TEXT_RANK = "bm25(articles_fts, 8.0, 3.0, 1.0)"

# FTS5 marks its hits with these. STX/ETX never occur in scraped article text,
# and asking FTS5 for '<mark>' directly would mean reaching for |safe on
# third-party HTML — see app.highlighted.
HL_OPEN, HL_CLOSE = "\x02", "\x03"

_SNIPPETS = f"""
               highlight(articles_fts, 0, char(2), char(3)) AS title_hl,
               snippet(articles_fts, 2, char(2), char(3), '…', 18) AS body_snippet,
               {_TEXT_RANK} AS text_rank
"""

_CARD_COLUMNS = """
        a.id, a.source, a.title, a.link, a.summary, a.published_at,
        a.published_day, a.paywalled, a.image_url,
        LENGTH(a.body_excerpt) AS body_chars,
        v.score, v.stage, v.kind, v.topics, v.rationale,
        (SELECT COUNT(DISTINCT a2.source)
           FROM articles a2
           JOIN verdicts v2 ON v2.article_id = a2.id
          WHERE v2.cluster_id = v.cluster_id
            AND a2.source != a.source) AS other_sources
"""


@dataclass
class FeedPage:
    """One rendered page of the feed, in whichever of the two shapes applies."""

    mode: str = "time"  # 'time' → day groups; 'rank' → a flat relevance list
    days: list[tuple[str, list[dict]]] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    total: int = 0
    day_totals: dict[str, int] = field(default_factory=dict)


def _predicates(q: Query, *, drop: str = "") -> tuple[str, list]:
    """The structured half of a query as SQL. `drop` blanks one facet.

    Blanking is what makes a facet count mean «how many would there be if I
    picked this instead», which is the only count worth printing next to a
    filter you have not chosen yet.
    """
    sql = " AND v.relevant = 1"
    params: list = []

    min_score = config.FEED_MIN_SCORE if q.min_score is None else q.min_score
    if drop != "score":
        sql += " AND v.score >= ?"
        params.append(min_score)
        if q.max_score is not None:
            sql += " AND v.score <= ?"
            params.append(q.max_score)

    if q.stages and drop != "trinn":
        sql += f" AND v.stage IN ({','.join('?' * len(q.stages))})"
        params += list(q.stages)
    if q.kinds and drop != "type":
        sql += f" AND v.kind IN ({','.join('?' * len(q.kinds))})"
        params += list(q.kinds)
    if drop != "kilde":
        if q.sources:
            sql += f" AND a.source IN ({','.join('?' * len(q.sources))})"
            params += list(q.sources)
        if q.not_sources:
            sql += f" AND a.source NOT IN ({','.join('?' * len(q.not_sources))})"
            params += list(q.not_sources)
    if drop != "dekning":
        for status in q.coverage:
            frag, extra = _coverage_predicate(status, negate=False)
            sql += frag
            params += extra
        for status in q.not_coverage:
            frag, extra = _coverage_predicate(status, negate=True)
            sql += frag
            params += extra

    if q.since_day:
        sql += " AND a.published_day >= ?"
        params.append(q.since_day)
    if q.on_day:
        sql += " AND a.published_day = ?"
        params.append(q.on_day)
    if q.paywalled is not None:
        sql += " AND COALESCE(a.paywalled, 0) = ?"
        params.append(int(q.paywalled))
    if q.cluster_id:
        sql += " AND v.cluster_id = ?"
        params.append(q.cluster_id)
    if drop != "skjul":
        for term in q.hidden:
            frag, extra = _mute_clause(term)
            sql += f" AND NOT {frag}"
            params += extra
    return sql, params


# The rowids one MATCH expression finds. A subquery rather than a join, so it
# drops into every predicate list — including the text search, whose FROM is
# already driven by a different MATCH on the same table.
_MATCHING = "SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?"


def _mute_clause(term: str) -> tuple[str, list]:
    """SQL that is true when a muted word marks this story as one to hide.

    Title and ingress through the index, and then the three places the
    classifier wrote down what the story is about: its rationale, its topics
    and the label of the running story it was filed under. PISA week showed
    why the index alone is not enough — «Kollaps for norske elevers
    ferdigheter» never says PISA, but the rationale, the topics and the cluster
    all do, and a reader who muted PISA meant that story too.

    LIKE rather than a second FTS index: these are short model-written fields,
    the scan is bounded by the predicates already applied, and a substring is
    the right match for a word the model may have spelled Pisa, PISA or
    Pisa-tall.
    """
    like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = (
        f"(a.rowid IN ({_MATCHING})"
        " OR v.rationale LIKE ? ESCAPE '\\'"
        " OR v.topics LIKE ? ESCAPE '\\'"
        " OR EXISTS (SELECT 1 FROM clusters cl"
        "             WHERE cl.id = v.cluster_id AND cl.label LIKE ? ESCAPE '\\'))"
    )
    return sql, [mute_match(term), like, like, like]


def _coverage_predicate(status: str, *, negate: bool) -> tuple[str, list]:
    """`dekning:` as SQL. ja/nei are the roll-up; the rest name a status."""
    if status in ("ja", "nei"):
        covered = (status == "ja") != negate
        return (f" AND {'' if covered else 'NOT '}{COVERED}", [])
    exists = (
        "EXISTS (SELECT 1 FROM coverage c2 WHERE c2.article_id = a.id AND c2.status = ?)"
    )
    return (f" AND {'NOT ' if negate else ''}{exists}", [status])


def today_oslo() -> str:
    """The Norwegian date `published_day` is bucketed against."""
    return datetime.now(config.OSLO).date().isoformat()


def _order_by(q: Query) -> tuple[str, list]:
    """How the page is ordered, and any parameter that ordering binds.

    Today is ordered newest first; every earlier day keeps the best story on
    top. The two answer different questions — today is still happening, so what
    the desk wants is what just landed, while yesterday is finished and what
    they want is what mattered. The CASE collapses to a constant on today's
    rows, which drops them straight through to the clock.
    """
    if q.mode == "rank" and q.match:
        # Referencing the alias inside an expression is a SQLite extension, and
        # it is what evaluates bm25() once per row instead of twice.
        return ("ORDER BY text_rank * (1.0 + v.score / 100.0) ASC, a.published_at DESC", [])
    if q.mode == "rank":
        return ("ORDER BY v.score DESC, a.published_at DESC", [])
    return (
        "ORDER BY a.published_day DESC, "
        "CASE WHEN a.published_day = ? THEN 0 ELSE v.score END DESC, "
        "a.published_at DESC",
        [today_oslo()],
    )


def _from_clause(q: Query) -> str:
    """The join order, which for a text query is the whole ballgame.

    CROSS JOIN is not decoration: with a plain JOIN the planner is free to put
    `verdicts` outermost and re-evaluate the MATCH per row. CROSS JOIN is
    SQLite's documented way to forbid reordering, and the FTS table is the only
    one here that can drive the loop.
    """
    if q.match:
        return f"""
          FROM articles_fts
          CROSS JOIN articles a ON a.rowid = articles_fts.rowid
          CROSS JOIN verdicts v ON v.article_id = a.id
          {_COVERAGE_JOIN}
         WHERE articles_fts MATCH ?
        """
    return f"""
          FROM articles a
          JOIN verdicts v ON v.article_id = a.id
          {_COVERAGE_JOIN}
         WHERE 1 = 1
    """


def _count_from(q: Query, *, drop: str = "") -> tuple[str, list]:
    """FROM+WHERE for a counting query — no coverage join, no aux functions."""
    where, params = _predicates(q, drop=drop)
    if q.match:
        return (
            "FROM articles_fts "
            "CROSS JOIN articles a ON a.rowid = articles_fts.rowid "
            "CROSS JOIN verdicts v ON v.article_id = a.id "
            "WHERE articles_fts MATCH ?" + where,
            [q.match, *params],
        )
    return (
        "FROM articles a JOIN verdicts v ON v.article_id = a.id WHERE 1 = 1" + where,
        params,
    )


def _row_to_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["topics"] = json.loads(row["topics"]) if row["topics"] else []
    return item


def total(conn: sqlite3.Connection, q: Query) -> int:
    frm, params = _count_from(q)
    return conn.execute(f"SELECT COUNT(*) {frm}", params).fetchone()[0]


def feed(conn: sqlite3.Connection, q: Query) -> FeedPage:
    """One page of the feed, grouped by day or ranked by relevance."""
    n = total(conn, q)
    pages = max(1, -(-n // PER_PAGE))
    page = min(max(1, q.page), pages)
    offset = (page - 1) * PER_PAGE

    where, params = _predicates(q)
    head = [q.match] if q.match else []
    order_sql, order_params = _order_by(q)
    sql = f"""
        SELECT {_CARD_COLUMNS}, {_COVERAGE_SELECT}
               {"," + _SNIPPETS if q.match else ""}
        {_from_clause(q)}
        {where}
        {order_sql}
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(
        sql, [*head, *params, *order_params, PER_PAGE, offset]
    ).fetchall()
    items = [_row_to_item(r) for r in rows]

    if q.mode == "rank":
        return FeedPage(mode="rank", items=items, total=n)

    # Day totals come from an unpaginated count, because a page boundary cuts a
    # day in half and «12 saker» over a day showing three is a wrong number, not
    # a small one.
    frm, cparams = _count_from(q)
    day_totals = {
        r["day"]: r["n"]
        for r in conn.execute(
            f"SELECT a.published_day AS day, COUNT(*) n {frm} GROUP BY a.published_day",
            cparams,
        )
    }
    days = [
        (day, list(group))
        for day, group in groupby(items, key=lambda it: it["published_day"])
    ]
    return FeedPage(mode="time", days=days, total=n, day_totals=day_totals)


def facet_counts(conn: sqlite3.Connection, q: Query) -> dict[str, dict[str, int]]:
    """Per-value totals for every facet, each ignoring its own filter.

    Four grouped queries rather than the previous four helpers plus two extra
    scans. The MATCH stays in every one of them: «Barnehage 12» has to mean 12
    rows matching the text, not 12 rows in the corpus.
    """
    out: dict[str, dict[str, int]] = {}
    for facet, column in (("trinn", "v.stage"), ("type", "v.kind"), ("kilde", "a.source")):
        frm, params = _count_from(q, drop=facet)
        out[facet] = {
            r[0]: r[1]
            for r in conn.execute(f"SELECT {column}, COUNT(*) {frm} GROUP BY 1", params)
            if r[0]
        }

    frm, params = _count_from(q, drop="dekning")
    row = conn.execute(
        f"SELECT SUM(CASE WHEN {COVERED} THEN 1 ELSE 0 END) AS ja, COUNT(*) AS alle {frm}",
        params,
    ).fetchone()
    ja = row["ja"] or 0
    out["dekning"] = {"ja": ja, "nei": (row["alle"] or 0) - ja}
    return out


def hidden_counts(conn: sqlite3.Connection, q: Query) -> dict[str, int]:
    """How many stories each muted word is keeping off this page's query.

    Counted against everything else the reader asked for, so the number is
    «what comes back if I unmute this», not a corpus-wide total. Two words that
    hit the same story both count it — the alternative is a number that changes
    when the reader unmutes a different word.
    """
    frm, params = _count_from(q, drop="skjul")
    out = {}
    for term in q.hidden:
        frag, extra = _mute_clause(term)
        out[term] = conn.execute(f"SELECT COUNT(*) {frm} AND {frag}", [*params, *extra]).fetchone()[0]
    return out


def all_sources(conn: sqlite3.Connection) -> list[str]:
    """Every source name in the database — the `kilde:` vocabulary."""
    return [r[0] for r in conn.execute("SELECT DISTINCT source FROM articles ORDER BY source") if r[0]]


def source_stats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Per-source yield: how much each source gives us, and how much of it lands.

    Ordered by relevant count, because the question this answers is which
    sources actually produce stories — not which ones are loudest. A source with
    a thousand articles and four relevant ones is worth knowing about, and the
    share column is what makes that visible.

    LEFT JOIN, so articles still queued for classification are counted in the
    total rather than silently dropped from it.
    """
    return conn.execute(
        """
        SELECT a.source,
               COUNT(*) AS articles,
               SUM(CASE WHEN v.relevant = 1 THEN 1 ELSE 0 END) AS relevant,
               SUM(CASE WHEN v.relevant = 1 AND v.score >= 60 THEN 1 ELSE 0 END) AS strong,
               AVG(CASE WHEN v.relevant = 1 THEN v.score END) AS avg_score,
               MAX(CASE WHEN v.relevant = 1 THEN a.published_day END) AS last_hit
          FROM articles a
          LEFT JOIN verdicts v ON v.article_id = a.id
         GROUP BY a.source
         ORDER BY relevant DESC, articles DESC, a.source
        """
    ).fetchall()


def get_article(conn: sqlite3.Connection, article_id: str) -> dict | None:
    """One story with its verdict, for the detail page."""
    row = conn.execute(
        f"""
        SELECT a.id, a.source, a.feed_name, a.title, a.link, a.summary, a.body_excerpt,
               a.published_at, a.published_day, a.paywalled, a.image_url, a.ingested_at,
               v.score, v.stage, v.kind, v.topics, v.rationale, v.relevant,
               v.model, v.classified_at, v.prefilter_terms, v.cluster_id,
               c.label AS cluster_label, c.first_day AS cluster_first_day,
               c.last_day AS cluster_last_day,
               {_COVERAGE_SELECT}
          FROM articles a
          JOIN verdicts v ON v.article_id = a.id
          LEFT JOIN clusters c ON c.id = v.cluster_id
          {_COVERAGE_JOIN}
         WHERE a.id = ?
        """,
        (article_id,),
    ).fetchone()
    if not row:
        return None
    item = _row_to_item(row)
    item["prefilter_terms"] = json.loads(row["prefilter_terms"]) if row["prefilter_terms"] else []
    return item


def story_timeline(conn: sqlite3.Connection, item: dict) -> list[dict]:
    """The running story this article belongs to, latest beat first.

    One entry per day the story was covered, each listing every source that
    carried it — so the reader sees both how long it has run and how widely it
    was picked up on any given day. Empty when the article stands alone.

    Newest first, and strictly by the clock inside a day: the reader opens a
    story to find out where it has got to, and a timeline that is not in time
    order is not a timeline.

    Only articles the classifier kept. Turned-down coverage used to be offered
    the open stories too, on the theory that the outlet which broke a story is
    often the one scored 0 — but the classifier gives a turned-down article no
    topics, so it was matched on recency alone and joined whatever was freshest.
    The filter stays as the guarantee, so a story left over from that never
    reaches the page.
    """
    cluster_id = item.get("cluster_id")
    if not cluster_id:
        return []

    rows = conn.execute(
        """
        SELECT a.id, a.source, a.title, a.link, a.published_at, a.published_day,
               v.score, v.relevant
          FROM articles a
          JOIN verdicts v ON v.article_id = a.id
         WHERE v.cluster_id = ? AND v.relevant = 1
         ORDER BY a.published_day DESC, a.published_at DESC, a.id
        """,
        (cluster_id,),
    ).fetchall()
    if len(rows) < 2:  # a story of one is just this article
        return []

    return [
        {
            "day": day,
            "articles": [{**dict(r), "current": r["id"] == item["id"]} for r in beats],
        }
        for day, beats in groupby(rows, key=lambda r: r["published_day"])
    ]


def story_coverage(item: dict) -> dict | None:
    """Our article that covers this one, in the shape the story page renders.

    «oppfolging» carries a second line instead of the rationale: the reader can
    see for themselves that the event is the same one, and what they cannot see
    is what this article added to it.
    """
    if item.get("coverage_status") not in ("sitert", "dekket", "oppfolging"):
        return None
    return {
        "level": item["coverage_status"],
        "url": item["internal_url"],
        "title": item["internal_title"],
        "day": item["internal_day"],
        "rationale": item.get("coverage_rationale"),
        "whats_new": item.get("coverage_whats_new"),
    }


def same_day(conn: sqlite3.Connection, item: dict, limit: int = 6) -> list[sqlite3.Row]:
    """The rest of the day, best first — so a story page isn't a dead end."""
    return list(
        conn.execute(
            """
            SELECT a.id, a.source, a.title, a.published_at, v.score, v.stage
              FROM articles a
              JOIN verdicts v ON v.article_id = a.id
             WHERE a.published_day = ? AND a.id != ? AND v.relevant = 1
             ORDER BY v.score DESC, a.published_at DESC
             LIMIT ?
            """,
            (item["published_day"], item["id"], limit),
        )
    )


def rejected(conn: sqlite3.Connection, *, days: int = 7, limit: int = 200) -> list[dict]:
    """What the radar turned down, newest first — so recall is inspectable.

    Both gates, in one list, because they fail differently: the model can be
    read (`rationale` says why), the word filter cannot say anything except
    which terms it did or did not find. A source that quietly stops appearing in
    the feed is invisible until someone can see what was thrown away.
    """
    cutoff = (
        conn.execute("SELECT date('now', ?)", (f"-{days} day",)).fetchone()[0]
    )
    rows = conn.execute(
        """
        SELECT a.id, a.source, a.title, a.link, a.summary, a.published_day, a.published_at,
               v.prefilter_passed, v.prefilter_reason, v.prefilter_terms,
               v.relevant, v.score, v.rationale, v.error
          FROM articles a
          JOIN verdicts v ON v.article_id = a.id
         WHERE a.published_day >= ?
           AND (v.prefilter_passed = 0 OR v.relevant = 0)
         ORDER BY a.published_day DESC, a.published_at DESC
         LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["prefilter_terms"] = json.loads(r["prefilter_terms"]) if r["prefilter_terms"] else []
        item["gate"] = "ordfilter" if not r["prefilter_passed"] else "modell"
        out.append(item)
    return out


def rejected_counts(conn: sqlite3.Connection, *, days: int = 7) -> dict[str, int]:
    cutoff = conn.execute("SELECT date('now', ?)", (f"-{days} day",)).fetchone()[0]
    row = conn.execute(
        """
        SELECT SUM(v.prefilter_passed = 0) AS ordfilter,
               SUM(v.prefilter_passed = 1 AND v.relevant = 0) AS modell,
               SUM(v.relevant = 1) AS beholdt
          FROM articles a JOIN verdicts v ON v.article_id = a.id
         WHERE a.published_day >= ?
        """,
        (cutoff,),
    ).fetchone()
    return {k: row[k] or 0 for k in ("ordfilter", "modell", "beholdt")}


def score_bands(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """How often each score band turned out to be something we cited.

    The only precision signal in the database that costs nothing to compute: a
    band whose articles we never cite is a band scored too high.
    """
    return list(
        conn.execute(
            """
            SELECT v.prompt_version, (v.score / 20) * 20 AS band, COUNT(*) AS n,
                   SUM(c.status = 'sitert') AS sitert,
                   SUM(c.status IN ('dekket', 'oppfolging')) AS dekket
              FROM verdicts v
              LEFT JOIN coverage c ON c.article_id = v.article_id
             WHERE v.relevant = 1 AND v.score IS NOT NULL
             GROUP BY 1, 2
             ORDER BY 1 DESC, 2 DESC
            """
        )
    )


def daily_volume(conn: sqlite3.Connection, q: Query, days: int = 21) -> list[sqlite3.Row]:
    """Relevant articles per day, oldest first — the sparkline.

    Reads the threshold off the live query rather than the config default, so
    the bars and the list below them are counting the same thing.
    """
    min_score = config.FEED_MIN_SCORE if q.min_score is None else q.min_score
    rows = conn.execute(
        """
        SELECT a.published_day AS day, COUNT(*) n
          FROM articles a JOIN verdicts v ON v.article_id = a.id
         WHERE v.relevant = 1 AND v.score >= ?
         GROUP BY a.published_day
         ORDER BY a.published_day DESC
         LIMIT ?
        """,
        (min_score, days),
    ).fetchall()
    return list(reversed(rows))


# stats() is a whole-database summary that only changes when a tick runs, and
# every page render asks for it. 60 s is shorter than the two-hour cron by three
# orders of magnitude and long enough that a reload costs nothing.
_STATS_TTL = 60.0
_stats_cache: tuple[float, dict] | None = None


def stats(conn: sqlite3.Connection | None = None, *, fresh: bool = False) -> dict:
    """Headline numbers for the rail and /stats."""
    global _stats_cache
    if not fresh and _stats_cache and time.monotonic() - _stats_cache[0] < _STATS_TTL:
        return _stats_cache[1]

    own = conn is None
    conn = conn or storage.connect()
    try:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM articles) AS ingested,
              (SELECT COUNT(*) FROM verdicts WHERE relevant = 1) AS relevant,
              (SELECT COUNT(*) FROM verdicts WHERE prefilter_passed = 1 AND classified_at IS NULL
                AND (error IS NULL OR error NOT LIKE 'blocked response:%'))
                AS pending,
              (SELECT COUNT(*) FROM verdicts WHERE prefilter_passed = 1 AND classified_at IS NULL
                AND error LIKE 'blocked response:%') AS blocked,
              (SELECT COUNT(*) FROM clusters) AS stories,
              (SELECT COUNT(*) FROM verdicts WHERE relevant = 1 AND cluster_id IS NULL)
                AS unclustered,
              (SELECT COUNT(*) FROM coverage WHERE status = 'sitert') AS cited,
              (SELECT COUNT(*) FROM coverage WHERE status IN ('dekket', 'oppfolging'))
                AS covered,
              (SELECT COUNT(*) FROM verdicts
                WHERE classified_at IS NOT NULL AND prompt_version IS NOT ?) AS stale_prompt,
              -- Only a clean run counts as «oppdatert». A tick that raised still
              -- wrote its row, and reading MAX(finished_at) off that told the
              -- desk the radar was current at exactly the moment it wasn't.
              (SELECT MAX(finished_at) FROM tick_runs
                WHERE error IS NULL AND COALESCE(errors, 0) = 0) AS last_tick,
              -- Stories are committed individually, even when a later model
              -- call fails. Publication dates are not evidence of processing.
              (SELECT MAX(classified_at) FROM verdicts
                WHERE relevant = 1) AS last_story_update,
              (SELECT MAX(finished_at) FROM tick_runs) AS last_attempt,
              (SELECT error FROM tick_runs ORDER BY id DESC LIMIT 1) AS last_error,
              (SELECT COALESCE(errors, 0) FROM tick_runs ORDER BY id DESC LIMIT 1)
                AS last_errors,
              (SELECT archive_seen FROM tick_runs
                WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1) AS archive_seen,
              (SELECT COALESCE(SUM(llm_calls), 0) FROM tick_runs
                WHERE started_at >= date('now', '-7 day')) AS llm_calls_7d,
              (SELECT COALESCE(SUM(llm_tokens), 0) FROM tick_runs
                WHERE started_at >= date('now', '-7 day')) AS llm_tokens_7d
            """,
            (_prompt_version(),),
        ).fetchone()
        out = dict(row)
        out["last_updated"] = max(
            (value for value in (out["last_tick"], out["last_story_update"]) if value),
            default=None,
        )
    finally:
        if own:
            conn.close()
    _stats_cache = (time.monotonic(), out)
    return out


def tick_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """The last runs, newest first — the operator view of the pipeline.

    Read by the unyttkassa admin dashboard. It is deliberately not read by the
    feed: the desk needs to know the radar is stale, not which call failed.
    """
    rows = conn.execute(
        """
        SELECT id, started_at, finished_at, ingested, prefiltered_in, classified,
               relevant, clustered, new_stories, cited, covered,
               llm_calls, llm_tokens, archive_seen, errors, error
          FROM tick_runs
         ORDER BY id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _prompt_version() -> str:
    from .. import classify

    return classify.PROMPT_VERSION

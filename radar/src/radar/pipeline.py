"""The tick: ingest → lexical gate → LLM → store."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import classify, cluster, config, coverage, dagbladet, gemini, storage
from .archive import InternalArchive
from .prefilter import prefilter
from .source import ScraperSource
from .urls import is_page_anchor, normalize

logger = logging.getLogger(__name__)

CURSOR_KEY = "scraper_cursor"


class TickBusy(RuntimeError):
    """Another tick holds the lock. Not a failure — the cron fired twice."""


@dataclass
class TickResult:
    """What one run did, for logging and the /stats endpoint."""

    ingested: int = 0
    prefiltered_in: int = 0
    classified: int = 0
    relevant: int = 0
    clustered: int = 0
    new_stories: int = 0
    cited: int = 0
    covered: int = 0
    llm_calls: int = 0
    llm_tokens: int = 0
    errors: int = 0

    @property
    def achieved_nothing(self) -> bool:
        """Errors, and not one piece of work got through.

        The distinction the exit code needs. A single model call timing out is
        normal — it leaves `classified_at` NULL and the next tick picks it up,
        which is the design. A tick where *everything* failed is a wrong key, a
        dead API or a bad prompt version, and nobody finds out unless the
        process says so. Alerting on the first would train the desk to ignore
        the second.
        """
        return bool(self.errors) and not (
            self.classified or self.clustered or self.cited or self.covered
        )

    def line(self) -> str:
        return (
            f"ingested={self.ingested} passed_prefilter={self.prefiltered_in} "
            f"classified={self.classified} relevant={self.relevant} "
            f"clustered={self.clustered} new_stories={self.new_stories} "
            f"cited={self.cited} covered={self.covered} "
            f"llm_calls={self.llm_calls} llm_tokens={self.llm_tokens} "
            f"errors={self.errors}"
        )


def rewind_cursor() -> None:
    """Drop the watermark back to the seed window.

    Re-reads every article in the window, which re-runs the lexical gate and
    clears the verdicts — the way to pick up a change in how articles are read
    or stored, not just how they are scored.
    """
    conn = storage.connect()
    try:
        storage.set_state(conn, CURSOR_KEY, ScraperSource().initial_cursor())
    finally:
        conn.close()


# How many articles are written before the transaction is closed. One
# transaction across a whole 2000-article batch holds ~9 MB of dirty pages, which
# spills the page cache and escalates the writer to EXCLUSIVE. Under WAL a reader
# survives that; the commit cadence keeps the window short anyway, so a kill
# mid-tick costs a couple of hundred upserts rather than all of them.
_INGEST_COMMIT_EVERY = 200

# Verdicts are the expensive rows — each one is a paid model call. Committing as
# they land means a kill costs at most this many calls, not the whole batch.
_CLASSIFY_COMMIT_EVERY = 25


def _drop_trailing_tie(articles: list[dict], limit: int) -> list[dict]:
    """Trim the last cursor group off a batch that filled its LIMIT.

    The scraper stamps one timestamp per batch, so hundreds of rows share a
    cursor value — the largest group in the live database is 353. The cursor is
    the maximum value seen and the next fetch asks for strictly greater, so a
    LIMIT landing inside a group silently drops the rest of it, permanently and
    with no error. Dropping the partial group instead makes the next tick re-read
    it whole; upsert makes the overlap free.

    A batch whose every row shares one timestamp is taken as-is — trimming it
    would return nothing and the tick would never advance.
    """
    if len(articles) < limit:
        return articles
    last = articles[-1]["scraper_cursor"]
    trimmed = [a for a in articles if a["scraper_cursor"] != last]
    return trimmed or articles


def ingest(conn, source: ScraperSource, *, limit: int) -> tuple[int, int]:
    """Pull new/changed articles and run the lexical gate. Returns (ingested, passed)."""
    cursor = storage.get_state(conn, CURSOR_KEY) or source.initial_cursor()
    articles = _drop_trailing_tie(source.fetch_since(cursor, limit=limit), limit)
    if not articles:
        return 0, 0

    # Read off the batch as fetched, before anything is dropped below. A batch
    # that is nothing but anchors still has to move the cursor, or the tick
    # re-reads the same rows for ever.
    watermark = articles[-1]["scraper_cursor"]
    # An anchor's body is the page's, not the item's — see urls.is_page_anchor.
    # Storing it files a real headline over someone else's article, and the
    # classifier then scores that article and tags this one with its topics.
    # A Dagbladet studio post is the same failure by another route: the body
    # is the live blog's shell, not the post — see dagbladet.is_studio_post.
    articles = [
        a for a in articles
        if not is_page_anchor(a["link"]) and not dagbladet.is_studio_post(a["source"], a["link"])
    ]

    passed = 0
    for i, article in enumerate(articles, start=1):
        # Some front pages mint a new id every time they rewrite the teaser, so
        # the same article arrives three times under three headlines. The link
        # is the identity: a newcomer for a link we hold becomes an update to
        # that row — newest headline, newest body, and the claims and votes
        # already on it stay put.
        twin = storage.article_id_for_link(conn, article["source"], article["link"])
        if twin and twin != article["id"]:
            article = {**article, "id": twin}
        storage.upsert_article(conn, article)
        result = prefilter(
            source=article["source"],
            title=article["title"],
            summary=article["summary"],
            body=article["body_excerpt"],
        )
        storage.record_prefilter(conn, article["id"], result)
        passed += int(result.passed)
        if i % _INGEST_COMMIT_EVERY == 0:
            conn.commit()

    conn.commit()
    # Last, and only after the rows are durable: the watermark is the promise
    # that everything below it has been read.
    storage.set_state(conn, CURSOR_KEY, watermark)
    return len(articles), passed


def classify_pending(conn, *, limit: int | None = None) -> tuple[int, int, int]:
    """Score everything waiting. Returns (classified, relevant, errors).

    `limit` defaults to config.CLASSIFY_LIMIT so one tick cannot run away on a
    backlog — the same guard clustering and coverage already have. 0 drains it.
    """
    rows = storage.pending_classification(
        conn, limit=config.CLASSIFY_LIMIT if limit is None else limit
    )
    if not rows:
        return 0, 0, 0

    articles = [dict(r) for r in rows]
    with ThreadPoolExecutor(max_workers=config.CLASSIFY_WORKERS) as pool:
        verdicts = list(pool.map(classify.classify, articles))

    relevant = errors = blocked = 0
    for i, (article, verdict) in enumerate(zip(articles, verdicts, strict=True), start=1):
        storage.record_classification(
            conn,
            article["id"],
            relevant=verdict.relevant,
            score=verdict.score,
            stage=verdict.stage,
            kind=verdict.kind,
            topics=verdict.topics,
            rationale=verdict.rationale,
            model=config.GEMINI_MODEL,
            prompt_version=classify.PROMPT_VERSION,
            error=verdict.error,
        )
        relevant += int(bool(verdict.relevant))
        # A content block is a final answer, not a failure: it is never retried
        # and says nothing about key, API or prompt health. Counting it failed
        # a tick whose only article happened to be blocked.
        if verdict.error and verdict.error.startswith(gemini.BLOCKED_PREFIX):
            blocked += 1
            logger.info("classify: %s blocked by the model (%s)", article["id"], verdict.error)
        elif verdict.error:
            errors += 1
        if i % _CLASSIFY_COMMIT_EVERY == 0:
            conn.commit()
    conn.commit()

    remaining = storage.count_pending_classification(conn)
    if remaining:
        logger.info("classify backlog: %d article(s) left for the next tick", remaining)
    return len(articles) - errors - blocked, relevant, errors


def cluster_pending(conn, *, limit: int | None = None) -> tuple[int, int, int]:
    """Tie relevant articles to running stories. Returns (clustered, new, errors).

    Sequential on purpose, unlike classification: each article can found a story
    that the next one should be allowed to join, so the candidate list is re-read
    every iteration. At roughly fifteen relevant articles a day that costs
    nothing, and it is what keeps a morning's coverage from becoming five
    separate stories.

    `limit` defaults to config.CLUSTER_LIMIT so one tick can't run away on a
    backlog; 0 means drain the queue however long it takes.
    """
    articles = storage.pending_clustering(
        conn, limit=config.CLUSTER_LIMIT if limit is None else limit
    )
    if not articles:
        return 0, 0, 0

    clustered = new_stories = errors = 0
    for article in articles:
        candidates = cluster.shortlist(
            article["topics"],
            storage.open_clusters(conn, days=config.CLUSTER_WINDOW_DAYS),
            config.CLUSTER_CANDIDATES,
        )
        result = cluster.assign(article, candidates)
        if result.error:
            errors += 1
            continue

        day = article["published_day"]
        if result.is_new:
            cluster_id = storage.create_cluster(conn, result.label, day)
            new_stories += 1
        else:
            cluster_id = candidates[int(result.handle[1:]) - 1]["id"]
        storage.attach_to_cluster(conn, article["id"], cluster_id, day)
        conn.commit()
        clustered += 1

    # A cap that quietly leaves work behind looks exactly like a working system.
    remaining = storage.count_pending_clustering(conn)
    if remaining:
        logger.info("cluster backlog: %d article(s) left for the next tick", remaining)

    return clustered, new_stories, errors


def coverage_pending(
    conn, *, limit: int | None = None, links_only: bool = False
) -> tuple[int, int, int]:
    """Work out what of this we have already written. Returns (cited, covered, errors).

    Runs after clustering, over relevant articles inside
    `COVERAGE_WINDOW_DAYS`. Two passes with very different economics: the link
    check is free and runs over the whole window on every tick, the model check
    is paid and gets at most two attempts per article.

    A missing archive is a supported state, not a failure — radar predates the
    mount, and a local checkout may never have one.
    """
    archive = InternalArchive()
    if not archive.available:
        # Not an error, but not nothing either. cited=0 covered=0 from a tick
        # that never looked is byte-identical to one that looked and found
        # nothing, and the feed would then tell the desk — in a filter pill with
        # a number beside it — that none of today's stories are ours. The run row
        # records which of the two happened; the feed reads it back.
        logger.warning("no archive at %s — coverage did not run", archive.db_path)
        return 0, 0, 0

    cited = _coverage_by_link(conn, archive)
    if links_only:
        return cited, 0, 0
    covered, errors = _coverage_by_model(conn, archive, limit=limit)
    return cited, covered, errors


def _coverage_by_link(conn, archive: InternalArchive) -> int:
    """Proof: an article of ours links straight at this one. Costs nothing.

    This is the pass that actually fires. We write the sitatsak hours after the
    radar first sees the source article and the archive picks it up the next
    morning, so the answer changes long after any one-shot check would have run.
    """
    index = archive.link_index(days=config.COVERAGE_WINDOW_DAYS)
    if not index:
        return 0

    hits = 0
    for row in storage.pending_coverage_links(conn, days=config.COVERAGE_WINDOW_DAYS):
        ours = index.get(normalize(row["link"]) or "")
        if not ours:
            continue
        storage.record_coverage(conn, row["id"], status="sitert", method="lenke", ours=ours)
        hits += 1
    conn.commit()
    return hits


def _coverage_by_model(
    conn, archive: InternalArchive, *, limit: int | None
) -> tuple[int, int]:
    """Everything we covered without linking it. Returns (covered, errors).

    Parallel, unlike clustering: each article is compared against a fixed
    snapshot of our own output, so nothing one call decides changes what the
    next one sees.
    """
    ours = archive.recent(days=config.COVERAGE_WINDOW_DAYS)
    if not ours:
        return 0, 0

    articles = storage.pending_coverage_check(
        conn,
        days=config.COVERAGE_WINDOW_DAYS,
        max_checks=config.COVERAGE_MAX_CHECKS,
        recheck_hours=config.COVERAGE_RECHECK_HOURS,
        limit=config.COVERAGE_LIMIT if limit is None else limit,
    )
    if not articles:
        return 0, 0

    def ask(article: dict) -> tuple[list[dict], coverage.Match]:
        candidates = coverage.shortlist(article, ours, config.COVERAGE_CANDIDATES)
        match = coverage.check(article, candidates)
        if not match.is_followup:
            return candidates, match
        # Paid for only where being wrong is expensive: the badge is about to
        # tell the desk there is something here we haven't got. Asked against
        # the whole window, because the first pass only saw the one article.
        return candidates, coverage.verify_novelty(
            match, ours, exclude=coverage.matched(candidates, match)
        )

    with ThreadPoolExecutor(max_workers=config.CLASSIFY_WORKERS) as pool:
        results = list(pool.map(ask, articles))

    covered = errors = 0
    for article, (candidates, match) in zip(articles, results, strict=True):
        if match.error:
            errors += 1
            storage.record_coverage(conn, article["id"], status="ingen", error=match.error)
            continue

        theirs = coverage.matched(candidates, match)
        # Covered either way — «oppfolging» says the event is ours and the
        # article still moved it on, which is a story to read, not to skip.
        status = "ingen"
        if not match.is_none:
            status = "oppfolging" if match.is_followup else "dekket"
        storage.record_coverage(
            conn,
            article["id"],
            status=status,
            method=None if match.is_none else "modell",
            ours=theirs,
            rationale=match.rationale,
            whats_new=match.whats_new,
            model=config.GEMINI_MODEL,
            prompt_version=coverage.PROMPT_VERSION,
            asked=True,
        )
        covered += int(theirs is not None)
    conn.commit()

    remaining = len(
        storage.pending_coverage_check(
            conn,
            days=config.COVERAGE_WINDOW_DAYS,
            max_checks=config.COVERAGE_MAX_CHECKS,
            recheck_hours=config.COVERAGE_RECHECK_HOURS,
        )
    )
    if remaining:
        logger.info("coverage backlog: %d article(s) left for the next tick", remaining)
    return covered, errors


def tick(
    *,
    ingest_limit: int = 2000,
    classify_limit: int | None = None,
    cluster_limit: int | None = None,
    coverage_limit: int | None = None,
    skip_llm: bool = False,
) -> TickResult:
    """One full pass. Safe to run more often than the scraper — no new rows, no work."""
    result = TickResult()
    conn = storage.connect()
    if not storage.acquire_tick_lock(conn):
        conn.close()
        raise TickBusy("another tick is still running")

    gemini.reset_usage()
    run_id = storage.log_tick_start(conn)
    archive_seen = InternalArchive().available
    error: str | None = None
    try:
        result.ingested, result.prefiltered_in = ingest(conn, ScraperSource(), limit=ingest_limit)
        if not skip_llm:
            result.classified, result.relevant, result.errors = classify_pending(
                conn, limit=classify_limit
            )
            result.clustered, result.new_stories, cluster_errors = cluster_pending(
                conn, limit=cluster_limit
            )
            result.cited, result.covered, coverage_errors = coverage_pending(
                conn, limit=coverage_limit
            )
            result.errors += cluster_errors + coverage_errors
    except Exception as e:  # noqa: BLE001 — recorded on the run row, then re-raised
        # Redacted: a transport error quotes the offending header back verbatim,
        # key included, and this string is written to tick_runs.error and read
        # back by /stats. gemini.py scrubs its own errors, but anything raised
        # below the GeminiError boundary — an httpx or google.auth exception —
        # arrives here untouched.
        error = gemini.redact(str(e))[:300]
        raise
    finally:
        spend = gemini.usage()
        result.llm_calls, result.llm_tokens = spend["calls"], spend["tokens"]
        storage.log_tick_finish(
            conn, run_id,
            ingested=result.ingested, prefiltered_in=result.prefiltered_in,
            classified=result.classified, relevant=result.relevant,
            clustered=result.clustered, new_stories=result.new_stories,
            cited=result.cited, covered=result.covered,
            llm_calls=result.llm_calls, llm_tokens=result.llm_tokens,
            archive_seen=int(archive_seen),
            errors=result.errors, error=error,
        )
        storage.release_tick_lock(conn)
        conn.close()
    return result

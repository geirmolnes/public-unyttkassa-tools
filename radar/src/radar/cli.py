"""Command-line entry points."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import classify, config, coverage, pipeline, storage


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    for noisy in ("httpx", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def tick() -> None:
    """Ingest from the scraper, gate, classify. Run from cron after each scrape."""
    p = argparse.ArgumentParser(description="Ingest and classify new articles.")
    p.add_argument("--ingest-limit", type=int, default=2000)
    p.add_argument("--classify-limit", type=int, default=None, help="Cap LLM calls this run.")
    p.add_argument("--cluster-limit", type=int, default=None,
                   help="Cap articles clustered this run; 0 drains the whole backlog.")
    p.add_argument("--coverage-limit", type=int, default=None,
                   help="Cap coverage checks this run; 0 drains the whole backlog.")
    p.add_argument("--no-llm", action="store_true", help="Ingest and gate only.")
    p.add_argument("--reingest", action="store_true",
                   help="Rewind the cursor to the seed window and re-read every article.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    _setup_logging(args.verbose)

    if args.reingest:
        pipeline.rewind_cursor()

    try:
        result = pipeline.tick(
            ingest_limit=args.ingest_limit,
            classify_limit=args.classify_limit,
            cluster_limit=args.cluster_limit,
            coverage_limit=args.coverage_limit,
            skip_llm=args.no_llm,
        )
    except pipeline.TickBusy as e:
        # Not a failure: the previous run is simply still going. Exit clean so
        # cron does not mail about it, but say so.
        print(f"skipped: {e}")
        return
    print(result.line())
    print(f"DB: {config.DB_PATH}")
    # Without this a tick where every model call failed exits 0 and looks, to
    # anything watching, like a tick that had nothing to do. A few failed calls
    # are not that: they retry on the next tick by design.
    if result.achieved_nothing:
        sys.exit(1)


def fetch() -> None:
    """Read the RSS feeds into the scraper database. Run from cron ahead of each tick."""
    p = argparse.ArgumentParser(description="Fetch RSS feeds and the article pages behind them.")
    p.add_argument("--feeds", type=Path, default=config.FEEDS_FILE,
                   help="Feed list, one «Kildenavn | https://…» per line.")
    p.add_argument("--body-limit", type=int, default=config.FETCH_BODY_LIMIT,
                   help="Cap article pages fetched this run.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    _setup_logging(args.verbose)

    try:
        # Not at the top of the file: feedparser and trafilatura are the optional
        # `rss` extra, and every other command has to run without them.
        from . import feeds
    except ImportError:
        sys.exit("radar-fetch needs the rss extra: uv sync --extra rss")
    if not args.feeds.exists():
        sys.exit(f"no feed list at {args.feeds} — copy feeds.example.txt to feeds.txt")

    conn = feeds.connect()
    try:
        result = feeds.run(feeds.read_feed_list(args.feeds), conn, body_limit=args.body_limit)
    finally:
        conn.close()
    print(result.line())
    print(f"DB: {config.SCRAPER_DB_PATH}")
    if result.achieved_nothing:
        sys.exit(1)


def doctor() -> None:
    """Check the things a deploy can silently get wrong.

    Exists because `dokku run radar python -c '…'` mangles quoting through the
    SSH wrapper, so there was no way to ask the running container whether the
    search index had actually built.
    """
    p = argparse.ArgumentParser(description="Post-deploy checks.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    _setup_logging(args.verbose)

    conn = storage.connect()
    problems: list[str] = []

    articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    indexed = conn.execute("SELECT COUNT(*) FROM articles_fts").fetchone()[0]
    print(f"articles      {articles}")
    print(f"indexed       {indexed}")
    if articles != indexed:
        problems.append(f"index is {indexed} rows against {articles} articles")

    try:
        conn.execute("INSERT INTO articles_fts(articles_fts) VALUES ('integrity-check')")
        print("integrity     ok")
    except sqlite3.DatabaseError as e:
        problems.append(f"integrity-check failed: {e}")

    hits = conn.execute(
        "SELECT COUNT(*) FROM articles_fts WHERE articles_fts MATCH ?", ('"skole"*',)
    ).fetchone()[0]
    print(f"match skole*  {hits}")
    if articles and not hits:
        problems.append("no article matches «skole*» — the tokenizer looks wrong")

    for pragma, want in (("journal_mode", "wal"), ("foreign_keys", 1)):
        got = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        print(f"{pragma:<13} {got}")
        if str(got).lower() != str(want).lower():
            problems.append(f"{pragma} is {got}, expected {want}")

    print(f"fts_version   {storage.get_state(conn, storage.FTS_VERSION_KEY)}")
    for name, path in (
        ("data", config.DB_PATH), ("scraper", config.SCRAPER_DB_PATH),
        ("archive", config.ARCHIVE_DB_PATH),
    ):
        state = "ok" if path.exists() else "MISSING"
        print(f"mount {name:<7} {state}  {path}")
        if name != "archive" and not path.exists():
            problems.append(f"{name} mount missing at {path}")
    conn.close()

    if problems:
        print("\nproblems:")
        for line in problems:
            print(f"  - {line}")
        sys.exit(1)
    print("\nall good")


def backup() -> None:
    """Snapshot the database to the volume, keeping the last N.

    SQLite's own online backup API rather than `cp`: the tick may be mid-write,
    and a copied file caught between two pages is a backup that restores to a
    corrupt database. This is also why it is Python and not the `sqlite3` CLI,
    which the slim image does not ship.
    """
    p = argparse.ArgumentParser(description="Back up the radar database.")
    p.add_argument("--dir", default=None, help="Where to write. Defaults to DATA_DIR/backups.")
    p.add_argument("--keep", type=int, default=7)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    _setup_logging(args.verbose)

    target_dir = Path(args.dir) if args.dir else config.DATA_DIR / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(config.OSLO).strftime("%Y-%m-%d")
    target = target_dir / f"utdanningsradar-{stamp}.db"

    source = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    kept = sorted(target_dir.glob("utdanningsradar-*.db"), reverse=True)
    for stale in kept[args.keep:]:
        stale.unlink()
    print(f"backed up to {target} ({target.stat().st_size // 1024} KB), {len(kept[:args.keep])} kept")


def prune() -> None:
    """Drop articles too old to be reachable, keeping proven citations."""
    p = argparse.ArgumentParser(description="Delete articles past the retention window.")
    p.add_argument("--days", type=int, default=storage.PRUNE_AFTER_DAYS)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    _setup_logging(args.verbose)

    conn = storage.connect()
    if args.dry_run:
        cutoff = (datetime.now(config.OSLO).date() - timedelta(days=args.days)).isoformat()
        n = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE published_day < ? "
            "AND id NOT IN (SELECT article_id FROM coverage WHERE status = 'sitert')",
            (cutoff,),
        ).fetchone()[0]
        print(f"would delete {n} articles published before {cutoff}")
    else:
        print(f"deleted {storage.prune(conn, days=args.days)} articles")
    conn.close()


def reclassify() -> None:
    """Re-run the LLM over stored articles — after a prompt or model change."""
    p = argparse.ArgumentParser(description="Clear verdicts and classify again.")
    p.add_argument("--since", help="Only articles published on/after this ISO date.")
    p.add_argument("--stale-prompt", action="store_true",
                   help="Only articles scored by an older prompt version.")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    _setup_logging(args.verbose)

    conn = storage.connect()
    sql = "UPDATE verdicts SET classified_at = NULL, error = NULL WHERE prefilter_passed = 1"
    params: list = []
    if args.stale_prompt:
        sql += " AND (prompt_version IS NULL OR prompt_version != ?)"
        params.append(classify.PROMPT_VERSION)
    if args.since:
        sql += " AND article_id IN (SELECT id FROM articles WHERE published_at >= ?)"
        params.append(args.since)
    cleared = conn.execute(sql, params).rowcount
    conn.commit()
    print(f"queued {cleared} articles for re-classification")

    classified, relevant, errors = pipeline.classify_pending(conn, limit=args.limit)
    conn.close()
    print(f"classified={classified} relevant={relevant} errors={errors}")
    if errors:
        sys.exit(1)


def recluster() -> None:
    """Rebuild the running stories from scratch — after a prompt change.

    Every other step can simply be asked again, because each article is judged
    on its own. Clustering isn't: an article is placed against the stories that
    happened to exist when it arrived, so a story that grew into a theme stays a
    theme and keeps pulling articles in. The stories have to be forgotten and
    built again in one pass, oldest article first.
    """
    p = argparse.ArgumentParser(description="Forget the story assignments and cluster again.")
    p.add_argument("--since", help="Only articles published on/after this ISO date.")
    p.add_argument("--limit", type=int, default=0, help="0 rebuilds the whole backlog.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    _setup_logging(args.verbose)

    conn = storage.connect()
    cleared, dropped = storage.clear_cluster_assignments(conn, since=args.since)
    print(f"cleared {cleared} articles, dropped {dropped} stories")

    clustered, new_stories, errors = pipeline.cluster_pending(conn, limit=args.limit)
    print(f"clustered={clustered} new={new_stories} errors={errors}")
    conn.close()
    if errors:
        sys.exit(1)


def recover() -> None:
    """Re-run the "have we written this?" step over articles already stored.

    The tick handles the steady state. This is for the two moments it can't: the
    archive was backfilled, or the prompt changed and the old answers no longer
    mean what they say.
    """
    p = argparse.ArgumentParser(description="Check our own archive for coverage again.")
    p.add_argument("--since", help="Only articles published on/after this ISO date.")
    p.add_argument("--stale-prompt", action="store_true",
                   help="Only articles answered by an older prompt version.")
    p.add_argument("--links-only", action="store_true",
                   help="Only the free link check — no model calls.")
    p.add_argument("--limit", type=int, default=0, help="0 drains the whole backlog.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    _setup_logging(args.verbose)

    conn = storage.connect()
    if not args.links_only:
        # Dropping the row rather than resetting `checks` gives the article its
        # full budget back, which is the point of asking again.
        sql = "DELETE FROM coverage WHERE status != 'sitert'"
        params: list = []
        if args.stale_prompt:
            sql += " AND (prompt_version IS NULL OR prompt_version != ?)"
            params.append(coverage.PROMPT_VERSION)
        if args.since:
            sql += " AND article_id IN (SELECT id FROM articles WHERE published_at >= ?)"
            params.append(args.since)
        cleared = conn.execute(sql, params).rowcount
        conn.commit()
        print(f"queued {cleared} articles for a fresh coverage check")

    cited, covered, errors = pipeline.coverage_pending(
        conn, limit=args.limit, links_only=args.links_only
    )
    conn.close()
    print(f"cited={cited} covered={covered} errors={errors}")
    if errors:
        sys.exit(1)

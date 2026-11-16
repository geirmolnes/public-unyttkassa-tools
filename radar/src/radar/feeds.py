"""A small RSS reader that fills the scraper database.

The pipeline reads articles out of a SQLite file (`source.py`). In our own
setup a separate scraper writes that file. This module writes the same
`articles` table from a list of RSS/Atom feeds, so an install with no scraper
needs nothing else: `radar-fetch`, then `radar-tick`.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import sqlite3
import time
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import feedparser
import trafilatura

from . import config, storage

logger = logging.getLogger(__name__)

Fetch = Callable[[str], bytes]
Clock = Callable[[], datetime]

_TIMEOUT_SECONDS = 20
_MAX_RESPONSE_BYTES = 5_000_000

# How many runs ask for a page that keeps failing. At a run every half hour this
# rides out a three-hour outage; a dead link stops costing a slot after that.
_BODY_MAX_ATTEMPTS = 6

_FETCH_ERRORS = (OSError, http.client.HTTPException, ValueError)

# The columns ScraperSource reads, plus body_attempts for the retry rule here.
_COLUMNS = frozenset(
    {
        "id", "source", "feed_name", "title", "link", "summary", "body_markdown",
        "published_at", "image_url", "paywalled", "changed_at", "first_seen_at",
        "body_fetched_at", "body_attempts",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    feed_name TEXT,
    title TEXT,
    link TEXT,
    summary TEXT,
    body_markdown TEXT,
    published_at TEXT,
    image_url TEXT,
    paywalled INTEGER,
    changed_at TEXT,
    first_seen_at TEXT NOT NULL,
    body_fetched_at TEXT,
    body_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_body_pending
    ON articles(first_seen_at) WHERE body_fetched_at IS NULL;
"""

# `changed_at` moves only when the headline or the summary does — that is what
# makes the pipeline score the article again. `paywalled` stays NULL: a feed
# does not say, and the feed's filter reads NULL as «not known to be».
_UPSERT = """
    INSERT INTO articles (id, source, feed_name, title, link, summary, published_at,
                          image_url, changed_at, first_seen_at)
    VALUES (:id, :source, :feed_name, :title, :link, :summary, :published_at,
            :image_url, :stamp, :stamp)
    ON CONFLICT(id) DO UPDATE SET
        title = excluded.title,
        summary = excluded.summary,
        link = excluded.link,
        published_at = excluded.published_at,
        image_url = excluded.image_url,
        changed_at = excluded.changed_at
    WHERE articles.title IS NOT excluded.title OR articles.summary IS NOT excluded.summary
"""


class Feed(NamedTuple):
    source: str
    url: str


@dataclass
class FetchResult:
    """What one run did."""

    feeds: int = 0
    feeds_failed: int = 0
    entries: int = 0
    changed: int = 0
    bodies: int = 0
    bodies_failed: int = 0
    pruned: int = 0

    @property
    def achieved_nothing(self) -> bool:
        """Every feed failed — as opposed to a quiet run with nothing new."""
        return self.feeds > 0 and self.feeds_failed == self.feeds

    def line(self) -> str:
        return (
            f"feeds={self.feeds} feeds_failed={self.feeds_failed} entries={self.entries} "
            f"new_or_changed={self.changed} bodies={self.bodies} "
            f"bodies_failed={self.bodies_failed} pruned={self.pruned}"
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _is_http(url: str | None) -> bool:
    try:
        return urlsplit(url or "").scheme in ("http", "https")
    except ValueError:
        return False


def read_feed_list(path: Path) -> list[Feed]:
    """Parse «Kildenavn | https://…» lines. Blank lines and # comments are skipped."""
    feeds: list[Feed] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        source, _, url = (part.strip() for part in line.partition("|"))
        if not source or not _is_http(url):
            raise ValueError(f"{path}:{number}: expected «Kildenavn | https://…», got {raw!r}")
        feeds.append(Feed(source, url))
    return feeds


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the database ScraperSource reads, creating it on first use.

    Refuses a file whose `articles` table is someone else's: SCRAPER_DB_PATH is
    also where a real scraper's database lives, and this must never write to one.
    """
    path = db_path or config.SCRAPER_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    if existing and existing != _COLUMNS:
        conn.close()
        raise RuntimeError(f"{path} was not created by radar-fetch; refusing to write to it")
    conn.executescript(_SCHEMA)
    return conn


def fetch_url(url: str) -> bytes:
    """GET a feed or an article page."""
    # urlopen also speaks file:// and ftp://, and article links come out of
    # third-party feeds.
    if not _is_http(url):
        raise ValueError(f"not an http(s) URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": config.FETCH_USER_AGENT})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return response.read(_MAX_RESPONSE_BYTES)


def _published(entry: feedparser.FeedParserDict) -> datetime | None:
    """When the entry says it was published. feedparser hands back UTC."""
    parsed: time.struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _image(entry: feedparser.FeedParserDict) -> str | None:
    """The first image the entry declares."""
    thumbnails = [m.get("url") for m in entry.get("media_thumbnail", [])]
    media = [
        m.get("url")
        for m in entry.get("media_content", [])
        if m.get("medium") == "image" or m.get("type", "").startswith("image/")
    ]
    enclosures = [
        e.get("href") for e in entry.get("enclosures", []) if e.get("type", "").startswith("image/")
    ]
    return next((url for url in (*thumbnails, *media, *enclosures) if _is_http(url)), None)


def parse_feed(feed: Feed, raw: bytes, *, now: datetime) -> list[dict]:
    """The entries of one feed as article rows, minus those too old to be news."""
    parsed = feedparser.parse(raw)
    # feedparser raises on nothing. An error page parses to zero entries and no
    # detected format, which is the one thing that tells it from a quiet feed.
    if not parsed.get("version"):
        raise ValueError("response is not an RSS or Atom feed")

    oldest = now - timedelta(days=config.MAX_ARTICLE_AGE_DAYS)
    # Some CMSes stamp the same guid on every item. A guid seen twice in one feed
    # identifies nothing, and would fold distinct articles onto one row.
    guid_count = Counter(entry.get("id") for entry in parsed.entries)

    articles: list[dict] = []
    for entry in parsed.entries:
        link = entry.get("link")
        if not _is_http(link):
            continue
        published = _published(entry)
        if published and published < oldest:
            continue
        guid = entry.get("id")
        key = guid if guid and guid_count[guid] == 1 else hashlib.sha1(link.encode()).hexdigest()
        articles.append(
            {
                "id": f"{feed.source}:{key}",
                "source": feed.source,
                "feed_name": parsed.feed.get("title") or None,
                "title": entry.get("title"),
                "link": link,
                "summary": entry.get("summary"),
                "published_at": published.isoformat() if published else None,
                "image_url": _image(entry),
            }
        )
    return articles


def extract_body(page: bytes, url: str) -> str | None:
    """The article text of a page as markdown, or None when none was found."""
    return trafilatura.extract(page, url=url, output_format="markdown", include_comments=False)


def fetch_bodies(
    conn: sqlite3.Connection, fetch: Fetch, *, limit: int, clock: Clock = _utcnow
) -> tuple[int, int]:
    """Fetch the page behind each article that has no body yet. Returns (stored, failed).

    Newest story first, across all feeds, so a backlog the cap left behind never
    delays what was just published. A row the backlog kept waiting past the
    ingest window is left alone: the pipeline would not read it.
    """
    oldest = (clock() - timedelta(days=config.MAX_ARTICLE_AGE_DAYS)).isoformat()
    pending = conn.execute(
        "SELECT id, link FROM articles"
        " WHERE body_fetched_at IS NULL AND body_attempts < ? AND first_seen_at >= ?"
        " ORDER BY COALESCE(published_at, first_seen_at) DESC, id LIMIT ?",
        (_BODY_MAX_ATTEMPTS, oldest, limit),
    ).fetchall()

    stored = failed = 0
    for article_id, link in pending:
        try:
            body = extract_body(fetch(link), link)
        except _FETCH_ERRORS as e:
            logger.warning("page failed: %s (%s)", link, e)
            conn.execute(
                "UPDATE articles SET body_attempts = body_attempts + 1 WHERE id = ?", (article_id,)
            )
            conn.commit()
            failed += 1
            continue
        # Stamped when written, never with one time for the whole run: the
        # pipeline's cursor is the latest of changed_at and body_fetched_at and
        # asks for strictly greater. A tick that reads mid-run moves its
        # watermark to the stamps it saw, so a body landing later has to carry a
        # later one or it is never read. Committed per page for the same reason,
        # and so a killed run keeps what it fetched.
        conn.execute(
            "UPDATE articles SET body_markdown = ?, body_fetched_at = ? WHERE id = ?",
            (body, clock().isoformat(), article_id),
        )
        conn.commit()
        stored += 1
    return stored, failed


def prune(conn: sqlite3.Connection, *, now: datetime) -> int:
    """Drop rows the radar itself has let go of. Returns how many went.

    Undated rows stay: nothing but the id says they have been seen before, and a
    feed that still lists one would bring it back as new.
    """
    cutoff = (now - timedelta(days=storage.PRUNE_AFTER_DAYS)).isoformat()
    deleted = conn.execute(
        "DELETE FROM articles WHERE published_at IS NOT NULL AND first_seen_at < ?", (cutoff,)
    ).rowcount
    conn.commit()
    return deleted


def run(
    feeds: list[Feed],
    conn: sqlite3.Connection,
    *,
    fetch: Fetch = fetch_url,
    body_limit: int | None = None,
    clock: Clock = _utcnow,
) -> FetchResult:
    """Read every feed, store what is new or changed, then fetch the missing bodies."""
    result = FetchResult(feeds=len(feeds))
    for feed in feeds:
        try:
            articles = parse_feed(feed, fetch(feed.url), now=clock())
        except _FETCH_ERRORS as e:
            logger.warning("feed failed: %s %s (%s)", feed.source, feed.url, e)
            result.feeds_failed += 1
            continue
        stamp = clock().isoformat()
        result.entries += len(articles)
        result.changed += sum(
            conn.execute(_UPSERT, article | {"stamp": stamp}).rowcount for article in articles
        )
        conn.commit()

    limit = config.FETCH_BODY_LIMIT if body_limit is None else body_limit
    result.bodies, result.bodies_failed = fetch_bodies(conn, fetch, limit=limit, clock=clock)
    result.pruned = prune(conn, now=clock())
    return result

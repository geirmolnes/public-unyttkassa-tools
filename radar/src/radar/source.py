"""Reading articles out of the news-scraper.

The scraper's own SQLite file is opened read-only. Deliberately *not* the
published snapshot in data-chattern's uploads volume: publish overwrites that
file in place in production, so a reader can catch it half-written.

Everything the rest of the app needs goes through `fetch_since`, so swapping
this for an HTTP client against the scraper is a one-file change.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config
from .text import html_to_text

# Same threshold the scraper's publish step uses: below this a body is a stub or
# a paywall teaser, not something to classify or show.
MIN_BODY_CHARS = 500


def oslo_day(published_at: str | None, fallback: str = "") -> str:
    """Publication date in Norwegian local time.

    The feed groups by day, and a story filed at 23:30 UTC belongs to the next
    Norwegian day. Every database we read from stores UTC, so both readers —
    the scraper and our own archive — land their timestamps here.
    """
    stamp = published_at or fallback
    try:
        return datetime.fromisoformat(stamp).astimezone(config.OSLO).date().isoformat()
    except (TypeError, ValueError):
        return stamp[:10]

# The cursor has to move when the article becomes *ingestible*, not only when
# the feed entry changes. The scraper stamps `changed_at` on insert and on a
# content-hash change, and nothing else — `update_body` writes `body_fetched_at`
# alone. So an article that arrived as a stub under 500 chars and grew past the
# floor hours later carries a `changed_at` already behind our watermark, and a
# strict `>` never returns it again. That is silent, permanent loss of exactly
# the articles the radar exists to catch: a breaking story is a stub first.
#
# Taking the later of the two timestamps makes body resolution move the cursor,
# so the row re-enters the window on the next tick. The inner COALESCEs are
# required: SQLite's scalar MAX() returns NULL if any argument is NULL.
_CURSOR = "MAX(COALESCE(changed_at, first_seen_at), COALESCE(body_fetched_at, ''))"

_QUERY = f"""
    SELECT id, source, feed_name, title, link, summary, body_markdown,
           published_at, image_url, paywalled,
           {_CURSOR} AS cursor
      FROM articles
     WHERE body_markdown IS NOT NULL
       AND LENGTH(body_markdown) >= {MIN_BODY_CHARS}
       AND {_CURSOR} > ?
       AND COALESCE(published_at, first_seen_at) >= ?
     ORDER BY {_CURSOR}
     LIMIT ?
"""


class ScraperSource:
    """Read-only view of the news-scraper database."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or config.SCRAPER_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(f"scraper DB not found at {self.db_path}")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def fetch_since(self, cursor: str | None, *, limit: int = 2000) -> list[dict]:
        """Articles changed after `cursor`, oldest first, already truncated.

        `cursor` is the scraper's changed_at, which it stamps on insert and on
        every content change — so a re-written article comes back and gets
        re-scored.
        """
        window_start = (
            datetime.now(UTC) - timedelta(days=config.MAX_ARTICLE_AGE_DAYS)
        ).isoformat()
        # closing(), not the bare connection context manager: `with conn` opens a
        # transaction and commits it, it does not close the handle. On the story
        # page fetch_body runs per request, so leaking one there leaks a file
        # descriptor per view.
        with closing(self._connect()) as conn:
            rows = conn.execute(_QUERY, (cursor or "", window_start, limit)).fetchall()
        return [self._to_article(r) for r in rows]

    @staticmethod
    def _to_article(row: sqlite3.Row) -> dict:
        # Flatten before truncating: cutting HTML at 4000 chars can sever a tag,
        # and the reader should never see markup either way.
        body = html_to_text(row["body_markdown"]) or ""
        return {
            "id": row["id"],
            "source": row["source"],
            "feed_name": row["feed_name"],
            "title": row["title"],
            "link": row["link"],
            "summary": html_to_text(row["summary"]),
            "body_excerpt": body[: config.MAX_BODY_CHARS],
            "published_at": row["published_at"],
            "published_day": oslo_day(row["published_at"], row["cursor"]),
            "image_url": row["image_url"],
            "paywalled": row["paywalled"],
            "scraper_cursor": row["cursor"],
        }

    def fetch_body(self, article_id: str) -> str | None:
        """The untruncated body, for the detail page. None if the scraper can't answer.

        Our own copy is cut at MAX_BODY_CHARS — enough to classify on, not enough
        to read. The full text stays where it already lives rather than being
        duplicated into this database.
        """
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT body_markdown FROM articles WHERE id = ?", (article_id,)
                ).fetchone()
        except (OSError, sqlite3.Error):
            return None
        return row["body_markdown"] if row else None

    def initial_cursor(self) -> str:
        """Where a fresh install starts: recent past, no archive."""
        return (datetime.now(UTC) - timedelta(days=config.SEED_WINDOW_DAYS)).isoformat()

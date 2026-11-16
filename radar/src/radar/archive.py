"""Reading our own back catalogue out of the internal scraper.

`unytt_artikler.db` is what utdanningsnytt.no has published, mirrored by
`unytt_internal_scraper`. Radar opens it read-only across a `:ro` mount, the
same arrangement as the news scraper's database in `source.py`.

Two things come out of it. `link_index` is the cheap one and the strong one: the
outbound links in our own articles. A link to the exact URL of an article in the
radar *is* the sitatsak — no model has to be asked. `recent` is the fallback for
everything we covered without linking.

Every read is allowed to fail. A radar deploy that predates the mount, or a
local checkout with no archive at all, must still tick — it just stops answering
the question.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config
from .source import oslo_day
from .urls import normalize

logger = logging.getLogger(__name__)

# The lead is what describes an article to the coverage check: it carries the
# event, and a long body would crowd a twenty-strong candidate list.
LEAD_CHARS = 200

# The novelty check reads the whole text instead, because that is where the
# thing it is looking for hides — a named reaction sits in the middle of one of
# our roundups, under a headline about something else. Capped so one very long
# piece cannot fill the prompt on its own.
BODY_CHARS = 6000

# Sections that are not our reporting. A reader's op-ed cites whatever it argues
# against, and one such piece linking four barnehage stories would otherwise mark
# all four as covered — which is precisely the failure that costs the desk saker
# it should have written. Fagartikler and advertorials go the same way.
#
# A denylist rather than a list of news sections: if utdanningsnytt.no adds one,
# the new section counts as reporting until we say otherwise. Wrong in that
# direction only tells the desk about a story twice.
NOT_REPORTING = ("debatt", "fagartikler", "annonse")
_HOLES = ", ".join("?" * len(NOT_REPORTING))

_LINKS = f"""
    SELECT l.url, a.article_id, a.url AS article_url, a.title, a.date_published
      FROM external_links l
      JOIN articles a ON a.article_id = l.article_id
     WHERE a.date_published >= ?
       AND COALESCE(a.article_section, '') NOT IN ({_HOLES})
"""

_RECENT = f"""
    SELECT article_id, url, title, lead, body_text, keywords, article_section, date_published
      FROM articles
     WHERE date_published >= ?
       AND COALESCE(article_section, '') NOT IN ({_HOLES})
     ORDER BY date_published DESC
"""


class InternalArchive:
    """Read-only view of what Utdanningsnytt has already published."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or config.ARCHIVE_DB_PATH
        self._warned = False

    @property
    def available(self) -> bool:
        return self.db_path.exists()

    def _query(self, sql: str, days: int) -> list[sqlite3.Row]:
        """Rows published within `days`, or nothing at all if the archive won't open.

        The cutoff is a bare date. `date_published` is an ISO timestamp with a
        `Z` suffix, so comparing it against a full timestamp would put two
        different offset spellings next to each other; against a date prefix the
        string comparison is exactly the date comparison we mean.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            with conn:
                return conn.execute(sql, (cutoff, *NOT_REPORTING)).fetchall()
        except (OSError, sqlite3.Error) as e:
            if not self._warned:
                logger.warning("archive unreadable at %s: %s", self.db_path, e)
                self._warned = True
            return []

    def link_index(self, *, days: int) -> dict[str, dict]:
        """Normalised outbound link → the article of ours that carries it.

        Built whole and held for one tick: a few thousand rows, against which
        every article in the window is then a dict lookup.

        Where two of our articles link the same thing, the newest wins — a
        follow-up is the better answer to "where did we write about this".
        """
        index: dict[str, dict] = {}
        for row in self._query(_LINKS, days):
            key = normalize(row["url"])
            if not key:
                continue
            ours = self._to_ours(row, url=row["article_url"])
            kept = index.get(key)
            if kept is None or (ours["day"] or "") >= (kept["day"] or ""):
                index[key] = ours
        return index

    def recent(self, *, days: int) -> list[dict]:
        """Everything we published in the window, newest first, with its text."""
        return [
            self._to_ours(row, url=row["url"], lead=(row["lead"] or "")[:LEAD_CHARS])
            for row in self._query(_RECENT, days)
        ]

    @staticmethod
    def _to_ours(row: sqlite3.Row, *, url: str, lead: str | None = None) -> dict:
        ours = {
            "id": row["article_id"],
            "url": url,
            "title": row["title"],
            "day": oslo_day(row["date_published"]),
        }
        if lead is not None:
            ours |= {
                "lead": lead,
                "body": (row["body_text"] or "")[:BODY_CHARS],
                "keywords": row["keywords"] or "",
                "section": row["article_section"] or "",
            }
        return ours

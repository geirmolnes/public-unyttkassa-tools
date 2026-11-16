"""Environment-driven configuration."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

# Everything is stored in UTC; this is the timezone it is read and displayed in.
OSLO = ZoneInfo("Europe/Oslo")


def _path(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    return Path(value) if value else default


_HOME_DATA = Path("data")

DATA_DIR = _path("RADAR_DATA_DIR", _HOME_DATA)
# Filename predates the rename to radar; kept so the database on the Dokku
# volume does not have to be moved.
DB_PATH = DATA_DIR / "utdanningsradar.db"

# The news-scraper database, mounted read-only in production.
SCRAPER_DB_PATH = _path("SCRAPER_DB_PATH", _HOME_DATA / "news_scraper.db")

# The built-in RSS reader (feeds.py, `radar-fetch`) writes SCRAPER_DB_PATH itself
# when no separate scraper does. One «Kildenavn | https://…» per line.
FEEDS_FILE = _path("RADAR_FEEDS_FILE", Path("feeds.txt"))

# Sent with every feed and page request, so a site owner can see who is reading.
FETCH_USER_AGENT = os.environ.get("RADAR_USER_AGENT", "radar-rss/0.1 (newsroom RSS reader)")

# Most article pages fetched in one `radar-fetch` run. Bounds how long a run takes
# and how hard the first run over full feeds hits the sources; the rest wait.
FETCH_BODY_LIMIT = int(os.environ.get("FETCH_BODY_LIMIT", "150"))

# Our own back catalogue, from unytt_internal_scraper, also mounted read-only.
# Missing is a supported state: the coverage step just doesn't run.
ARCHIVE_DB_PATH = _path("ARCHIVE_DB_PATH", _HOME_DATA / "unytt_artikler.db")

# Stripped: a key with a stray newline is rejected by the transport as an
# illegal header value, and the error quotes the key back with the newline
# escaped — which slips straight past gemini.redact and into the log.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

# How far back the very first tick reaches. Deliberately short: this is a news
# monitor, the archive builds itself from here on.
SEED_WINDOW_DAYS = int(os.environ.get("SEED_WINDOW_DAYS", "7"))

# Articles older than this are never ingested, even if the scraper only just
# resolved their body — a two-week-old story is not news.
MAX_ARTICLE_AGE_DAYS = int(os.environ.get("MAX_ARTICLE_AGE_DAYS", "14"))

# Bodies are stored and classified truncated: the lede and first sections carry
# the topic, and the rest is tokens we pay for twice (storage and prompt).
MAX_BODY_CHARS = int(os.environ.get("MAX_BODY_CHARS", "4000"))
CLASSIFY_BODY_CHARS = int(os.environ.get("CLASSIFY_BODY_CHARS", "1800"))

# Clustering sees less of the body than classification does: placing a story
# next to the ones already running is a job for the lede, not the whole text.
CLUSTER_BODY_CHARS = int(os.environ.get("CLUSTER_BODY_CHARS", "900"))

# How far back a running story can go quiet and still be offered as a candidate.
# Long enough that a hearing in July and the bill in August stay one story.
CLUSTER_WINDOW_DAYS = int(os.environ.get("CLUSTER_WINDOW_DAYS", "45"))

# Most candidate stories shown to the model at once, best match first. Which of
# the open stories fill these slots is cluster.shortlist's job — this is not a
# cap on how many stories may be open.
#
# Measured over three weeks of real articles (32 that had to join an existing
# story, 55 stories open): topic ranking put the right story on a 20-slot list
# 31 times, a 15-slot list 30 times, and 25 slots recovered nothing more.
# Ordering by recency alone — what this did before — managed 21 at 20 slots.
CLUSTER_CANDIDATES = int(os.environ.get("CLUSTER_CANDIDATES", "20"))

# Most articles clustered in one tick. Clustering is sequential — one call per
# article — so this bounds how long a tick runs, not what it costs. A normal
# day's handful of relevant articles never reaches it; a backlog drains over a
# few ticks instead of one very long one.
CLUSTER_LIMIT = int(os.environ.get("CLUSTER_LIMIT", "60"))

# How far back we look for our own coverage of an article. Three weeks: past
# that a sitatsak is not what anyone means by "we already did this one".
COVERAGE_WINDOW_DAYS = int(os.environ.get("COVERAGE_WINDOW_DAYS", "21"))

# Most of our own articles shown to the model at once, best match first.
COVERAGE_CANDIDATES = int(os.environ.get("COVERAGE_CANDIDATES", "20"))

# Our own articles whose full text is put in front of the novelty check. Few,
# because they go in whole: this pass only runs when the badge is about to claim
# something is new, and it only has to contain the one article that already had
# it. Ranking over bodies is what puts that one on the list.
COVERAGE_NOVELTY_CANDIDATES = int(os.environ.get("COVERAGE_NOVELTY_CANDIDATES", "3"))

# How many times an article is offered to the model before we stop asking. Two,
# because we usually write the sitatsak hours after the radar first sees the
# source article, and the archive only picks it up the following morning. The
# free link check keeps running either way.
COVERAGE_MAX_CHECKS = int(os.environ.get("COVERAGE_MAX_CHECKS", "2"))
COVERAGE_RECHECK_HOURS = int(os.environ.get("COVERAGE_RECHECK_HOURS", "36"))

# Most coverage checks in one tick. Like CLUSTER_LIMIT this bounds how long a
# tick runs, not what the feature costs — the first run over a full window is
# the only time it bites, and the backlog drains over a few ticks.
COVERAGE_LIMIT = int(os.environ.get("COVERAGE_LIMIT", "60"))

# Same reasoning as CLUSTER_BODY_CHARS: recognising our own coverage is a job
# for the lede.
COVERAGE_BODY_CHARS = int(os.environ.get("COVERAGE_BODY_CHARS", "900"))

# Concurrent Gemini calls per tick.
CLASSIFY_WORKERS = int(os.environ.get("CLASSIFY_WORKERS", "8"))

# Most articles classified in one tick. Like CLUSTER_LIMIT and COVERAGE_LIMIT
# this bounds how long a tick runs and what one tick can spend — a backlog after
# a prompt change drains over a few ticks instead of one very long, very
# expensive one. 0 means drain the queue.
CLASSIFY_LIMIT = int(os.environ.get("CLASSIFY_LIMIT", "200"))

# The default floor in the feed. 0: the desk would rather scroll past fagstoff
# than never learn it was there — a filter that hides things by default hides
# them from someone who does not know the filter exists. Raise it in the panel
# under «Relevans» when you want only the tops.
FEED_MIN_SCORE = int(os.environ.get("FEED_MIN_SCORE", "0"))

# Where «Lag sitatsak» hands a story off. Both apps sit on .example.org and
# share the session cookie, so the link carries the reader straight in.
SITATKLADD_URL = os.environ.get("SITATKLADD_URL", "https://sitatkladd.example.org")

# The way back out. The radar is one tool on the unyttkassa desk, not a place
# you land in and have to use the browser's back button to leave.
UNYTTKASSA_URL = os.environ.get("UNYTTKASSA_URL", "https://example.org")

# Shared key the unyttkassa admin dashboard uses to poll every tool's /api/stats.
# Operator status lives there, not in the feed: a journalist has no use for
# «1 modellkall feilet», and putting it in front of them trains everyone to
# scroll past the banner that does matter.
EVALS_API_KEY = os.environ.get("EVALS_API_KEY", "")

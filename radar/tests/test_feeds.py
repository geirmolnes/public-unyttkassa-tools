"""The built-in RSS reader: what it stores, and that the pipeline can read it back.

Nothing here touches the network. The fetch function is injected, the clock is
a fake, and the page extraction is the real trafilatura on a local HTML string.
"""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.error import URLError

import pytest

from radar import cli, config, feeds
from radar.source import ScraperSource

NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
FEED = feeds.Feed("Avisa", "https://avisa.example/rss")

PARAGRAPH = (
    "Kommunestyret vedtok tirsdag kveld å legge ned to av de fem grunnskolene i "
    "kommunen, etter en debatt som varte i over fire timer og samlet fulle "
    "tilhørerbenker. Rektorene ved de berørte skolene fikk beskjeden samme kveld. "
)


def page(marker: str = "") -> bytes:
    """An article page long enough to clear the pipeline's 500-character floor."""
    paragraphs = "".join(f"<p>{marker} {PARAGRAPH} Avsnitt {n}.</p>" for n in range(6))
    return (
        "<html><head><title>Skolenedleggelse</title></head><body><article>"
        f"<h1>To skoler legges ned</h1>{paragraphs}</article></body></html>"
    ).encode()


def item(
    n: int,
    *,
    guid: str | None = "",
    title: str | None = None,
    summary: str = "Ingress",
    published: datetime | None = NOW - timedelta(hours=1),
    link: str | None = None,
    extra: str = "",
) -> str:
    """One RSS item. guid="" means «derive one from n», guid=None leaves it out."""
    guid = f"guid-{n}" if guid == "" else guid
    return (
        "<item>"
        f"<title>{title or f'Tittel {n}'}</title>"
        f"<link>{link or f'https://avisa.example/sak/{n}'}</link>"
        f"<description>{summary}</description>"
        + (f"<guid>{guid}</guid>" if guid else "")
        + (f"<pubDate>{format_datetime(published)}</pubDate>" if published else "")
        + extra
        + "</item>"
    )


def rss(*items: str, title: str = "Avisa – siste nytt") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>'
        f"<title>{title}</title>{''.join(items)}</channel></rss>"
    ).encode()


class Net:
    """Canned responses per URL. An exception as the response is raised."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.asked: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


class Ticking:
    """A clock that moves one second every time it is read."""

    def __init__(self, start: datetime = NOW):
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def at(moment: datetime):
    return lambda: moment


@pytest.fixture
def db(tmp_path):
    return tmp_path / "news_scraper.db"


@pytest.fixture
def conn(db):
    connection = feeds.connect(db)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def rows(conn, sql="SELECT * FROM articles ORDER BY id"):
    return [dict(r) for r in conn.execute(sql)]


# --- the feed list ------------------------------------------------------------


def test_the_feed_list_skips_comments_and_blank_lines(tmp_path):
    path = tmp_path / "feeds.txt"
    path.write_text(
        "# aviser\n\nNRK | https://www.nrk.no/toppsaker.rss\n"
        "  Kommunal Rapport|http://kr.example/rss?a=1#x  \n",
        encoding="utf-8",
    )
    assert feeds.read_feed_list(path) == [
        feeds.Feed("NRK", "https://www.nrk.no/toppsaker.rss"),
        feeds.Feed("Kommunal Rapport", "http://kr.example/rss?a=1#x"),
    ]


@pytest.mark.parametrize(
    "line",
    (
        "https://www.nrk.no/toppsaker.rss",
        "| https://www.nrk.no/toppsaker.rss",
        "NRK |",
        "NRK | ftp://nrk.no/rss",
        "NRK | file:///etc/hosts",
        "NRK | www.nrk.no/toppsaker.rss",
    ),
    ids=["no-name", "empty-name", "empty-url", "ftp", "file", "no-scheme"],
)
def test_a_malformed_line_names_its_line_number(tmp_path, line):
    path = tmp_path / "feeds.txt"
    path.write_text(f"# første linje\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"feeds\.txt:2: "):
        feeds.read_feed_list(path)


# --- parsing ------------------------------------------------------------------


def test_an_entry_becomes_an_article_row():
    published = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
    raw = rss(item(1, published=published, summary="Kort ingress"))
    assert feeds.parse_feed(FEED, raw, now=NOW) == [
        {
            "id": "Avisa:guid-1",
            "source": "Avisa",
            "feed_name": "Avisa – siste nytt",
            "title": "Tittel 1",
            "link": "https://avisa.example/sak/1",
            "summary": "Kort ingress",
            "published_at": "2026-11-16T09:30:00+00:00",
            "image_url": None,
        }
    ]


def test_a_local_publication_time_is_stored_as_utc():
    raw = rss(
        "<item><title>T</title><link>https://avisa.example/a</link>"
        "<pubDate>Mon, 16 Nov 2026 09:30:00 +0100</pubDate></item>"
    )
    assert feeds.parse_feed(FEED, raw, now=NOW)[0]["published_at"] == "2026-11-16T08:30:00+00:00"


def link_hash(link: str) -> str:
    return "Avisa:" + hashlib.sha1(link.encode()).hexdigest()


@pytest.mark.parametrize(
    "items,expected",
    (
        ([item(1), item(2)], ["Avisa:guid-1", "Avisa:guid-2"]),
        ([item(1, guid=None)], [link_hash("https://avisa.example/sak/1")]),
        # One guid on every item identifies none of them.
        (
            [item(1, guid="https://avisa.example/a/"), item(2, guid="https://avisa.example/a/")],
            [link_hash("https://avisa.example/sak/1"), link_hash("https://avisa.example/sak/2")],
        ),
        # The repeated guid falls back; the unique one beside it is still used.
        (
            [item(1, guid="x"), item(2, guid="x"), item(3)],
            [
                link_hash("https://avisa.example/sak/1"),
                link_hash("https://avisa.example/sak/2"),
                "Avisa:guid-3",
            ],
        ),
    ),
    ids=["guid", "no-guid", "constant-guid", "mixed"],
)
def test_article_ids(items, expected):
    assert [a["id"] for a in feeds.parse_feed(FEED, rss(*items), now=NOW)] == expected


@pytest.mark.parametrize(
    "age,kept",
    (
        (timedelta(days=14) - timedelta(seconds=1), True),
        (timedelta(days=14), True),
        (timedelta(days=14) + timedelta(seconds=1), False),
        (timedelta(0), True),
    ),
    ids=["just-inside", "exactly-14-days", "just-outside", "now"],
)
def test_entries_older_than_the_window_are_skipped(monkeypatch, age, kept):
    monkeypatch.setattr(config, "MAX_ARTICLE_AGE_DAYS", 14)
    raw = rss(item(1, published=NOW - age))
    assert bool(feeds.parse_feed(FEED, raw, now=NOW)) is kept


def test_an_undated_entry_is_kept():
    article = feeds.parse_feed(FEED, rss(item(1, published=None)), now=NOW)[0]
    assert (article["id"], article["published_at"]) == ("Avisa:guid-1", None)


@pytest.mark.parametrize(
    "link",
    ("file:///etc/hosts", "ftp://avisa.example/sak", "javascript:alert(1)", "/sak/1"),
)
def test_an_entry_without_an_http_link_is_dropped(link):
    assert feeds.parse_feed(FEED, rss(item(1, link=link)), now=NOW) == []


@pytest.mark.parametrize(
    "extra,expected",
    (
        ('<media:thumbnail url="https://img.example/t.jpg"/>', "https://img.example/t.jpg"),
        (
            '<media:content url="https://img.example/c.jpg" medium="image"/>',
            "https://img.example/c.jpg",
        ),
        (
            '<enclosure url="https://img.example/e.png" type="image/png" length="1"/>',
            "https://img.example/e.png",
        ),
        ('<enclosure url="https://img.example/lyd.mp3" type="audio/mpeg" length="1"/>', None),
        ('<media:content url="https://img.example/v.mp4" medium="video"/>', None),
        ('<media:thumbnail url="file:///etc/hosts"/>', None),
        ("", None),
    ),
    ids=["thumbnail", "media-image", "enclosure", "audio", "video", "not-http", "none"],
)
def test_the_image_an_entry_declares(extra, expected):
    assert feeds.parse_feed(FEED, rss(item(1, extra=extra)), now=NOW)[0]["image_url"] == expected


def test_an_empty_feed_is_not_an_error():
    assert feeds.parse_feed(FEED, rss(), now=NOW) == []


@pytest.mark.parametrize(
    "raw",
    (b"", b"<html><body>404</body></html>", b'{"error": "not found"}'),
    ids=["empty", "html-error-page", "json"],
)
def test_a_response_that_is_no_feed_raises(raw):
    with pytest.raises(ValueError, match="not an RSS or Atom feed"):
        feeds.parse_feed(FEED, raw, now=NOW)


def test_a_feed_cut_off_mid_item_keeps_the_items_that_arrived():
    whole = rss(item(1), item(2))
    cut = whole[: whole.index(b"<item>", whole.index(b"<item>") + 1) + 12]
    assert [a["id"] for a in feeds.parse_feed(FEED, cut, now=NOW)] == ["Avisa:guid-1"]


# --- storing and change detection ----------------------------------------------


def test_a_run_stores_the_article_with_its_body(conn):
    net = Net({FEED.url: rss(item(1)), "https://avisa.example/sak/1": page()})
    result = feeds.run([FEED], conn, fetch=net, clock=Ticking())

    assert result.line() == (
        "feeds=1 feeds_failed=0 entries=1 new_or_changed=1 bodies=1 bodies_failed=0 pruned=0"
    )
    (row,) = rows(conn)
    body = row.pop("body_markdown")
    assert "To skoler legges ned" in body and "Avsnitt 5." in body
    assert row == {
        "id": "Avisa:guid-1",
        "source": "Avisa",
        "feed_name": "Avisa – siste nytt",
        "title": "Tittel 1",
        "link": "https://avisa.example/sak/1",
        "summary": "Ingress",
        "published_at": "2026-11-16T11:00:00+00:00",
        "image_url": None,
        "paywalled": None,
        "changed_at": "2026-11-16T12:00:02+00:00",
        "first_seen_at": "2026-11-16T12:00:02+00:00",
        "body_fetched_at": "2026-11-16T12:00:04+00:00",
        "body_attempts": 0,
    }


@pytest.mark.parametrize(
    "second,changed",
    (
        (item(1), 0),
        (item(1, title="Ny tittel"), 1),
        (item(1, summary="Ny ingress"), 1),
        # A new address alone is the same article: nothing downstream reads differently.
        (item(1, link="https://avisa.example/sak/1-flyttet"), 0),
    ),
    ids=["unchanged", "title", "summary", "link-only"],
)
def test_only_a_new_headline_or_summary_counts_as_a_change(conn, second, changed):
    net = Net({FEED.url: rss(item(1)), "https://avisa.example/sak/1": page()})
    clock = Ticking()
    feeds.run([FEED], conn, fetch=net, clock=clock)
    (before,) = rows(conn)

    net.responses[FEED.url] = rss(second)
    result = feeds.run([FEED], conn, fetch=net, clock=clock)
    (after,) = rows(conn)

    assert result.changed == changed
    assert (after["changed_at"] != before["changed_at"]) is bool(changed)
    assert after["first_seen_at"] == before["first_seen_at"]
    # The page is fetched once, whatever the feed does afterwards.
    assert result.bodies == 0
    assert net.asked.count("https://avisa.example/sak/1") == 1
    assert after["body_markdown"] == before["body_markdown"]


def test_the_same_article_in_two_feeds_is_one_row(conn):
    second = feeds.Feed("Avisa", "https://avisa.example/rss/innenriks")
    net = Net(
        {
            FEED.url: rss(item(1)),
            second.url: rss(item(1), title="Avisa – innenriks"),
            "https://avisa.example/sak/1": page(),
        }
    )
    result = feeds.run([FEED, second], conn, fetch=net, clock=Ticking())

    assert (result.entries, result.changed) == (2, 1)
    assert [r["feed_name"] for r in rows(conn)] == ["Avisa – siste nytt"]


# --- failures -------------------------------------------------------------------


@pytest.mark.parametrize(
    "broken",
    (URLError("connection refused"), TimeoutError("timed out"), b"<html>503</html>"),
    ids=["unreachable", "timeout", "not-a-feed"],
)
def test_one_failing_feed_does_not_stop_the_run(conn, broken):
    dead = feeds.Feed("Nede", "https://nede.example/rss")
    net = Net({dead.url: broken, FEED.url: rss(item(1)), "https://avisa.example/sak/1": page()})
    result = feeds.run([dead, FEED], conn, fetch=net, clock=Ticking())

    assert (result.feeds, result.feeds_failed, result.changed) == (2, 1, 1)
    assert result.achieved_nothing is False
    assert [r["id"] for r in rows(conn)] == ["Avisa:guid-1"]


@pytest.mark.parametrize(
    "failing,total,nothing",
    ((0, 0, False), (0, 1, False), (1, 1, True), (1, 2, False), (2, 2, True)),
)
def test_a_run_achieved_nothing_only_when_every_feed_failed(failing, total, nothing):
    result = feeds.FetchResult(feeds=total, feeds_failed=failing)
    assert result.achieved_nothing is nothing


def test_a_page_with_no_article_in_it_is_not_asked_for_again(conn):
    net = Net({FEED.url: rss(item(1)), "https://avisa.example/sak/1": b"<html></html>"})
    clock = Ticking()
    first = feeds.run([FEED], conn, fetch=net, clock=clock)
    second = feeds.run([FEED], conn, fetch=net, clock=clock)

    assert (first.bodies, second.bodies) == (1, 0)
    (row,) = rows(conn)
    assert row["body_markdown"] is None and row["body_fetched_at"] is not None


@pytest.mark.parametrize(
    "failures,stored",
    ((0, True), (1, True), (5, True), (6, False), (7, False)),
    ids=["first-try", "second-try", "sixth-try", "gave-up", "long-dead"],
)
def test_a_failing_page_is_asked_for_six_times(conn, failures, stored):
    link = "https://avisa.example/sak/1"
    net = Net({FEED.url: rss(item(1)), link: URLError("down")})
    clock = Ticking()
    failed = [feeds.run([FEED], conn, fetch=net, clock=clock).bodies_failed for _ in range(failures)]
    assert failed == [1] * min(failures, 6) + [0] * max(failures - 6, 0)

    net.responses[link] = page()
    assert feeds.run([FEED], conn, fetch=net, clock=clock).bodies == int(stored)
    assert (rows(conn)[0]["body_markdown"] is not None) is stored


# The cap left this article waiting; whether a later run still fetches it
# depends on whether the pipeline would still read it.
@pytest.mark.parametrize(
    "waited,fetched",
    (
        (timedelta(days=14) - timedelta(seconds=1), 1),
        (timedelta(days=14), 1),
        (timedelta(days=14) + timedelta(seconds=1), 0),
    ),
    ids=["just-inside", "exactly-14-days", "just-outside"],
)
def test_a_body_that_waited_past_the_ingest_window_is_left(conn, monkeypatch, waited, fetched):
    monkeypatch.setattr(config, "MAX_ARTICLE_AGE_DAYS", 14)
    net = Net({FEED.url: rss(item(1, published=None)), "https://avisa.example/sak/1": page()})
    feeds.run([FEED], conn, fetch=net, body_limit=0, clock=at(NOW))

    assert feeds.run([FEED], conn, fetch=net, clock=at(NOW + waited)).bodies == fetched


@pytest.mark.parametrize("limit,fetched", ((0, 0), (1, 1), (2, 2), (3, 3), (4, 3)))
def test_the_body_cap(conn, limit, fetched):
    net = Net(
        {FEED.url: rss(item(1), item(2), item(3))}
        | {f"https://avisa.example/sak/{n}": page() for n in (1, 2, 3)}
    )
    result = feeds.run([FEED], conn, fetch=net, body_limit=limit, clock=Ticking())

    assert result.bodies == fetched
    stored = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE body_fetched_at IS NOT NULL"
    ).fetchone()[0]
    assert stored == fetched


def test_the_cap_spends_itself_on_the_newest_stories(conn):
    older, newest, middle = (NOW - timedelta(hours=h) for h in (9, 1, 5))
    net = Net(
        {FEED.url: rss(item(1, published=older), item(2, published=newest), item(3, published=middle))}
        | {f"https://avisa.example/sak/{n}": page() for n in (1, 2, 3)}
    )
    feeds.run([FEED], conn, fetch=net, body_limit=2, clock=Ticking())
    assert [r["id"] for r in rows(conn) if r["body_fetched_at"]] == ["Avisa:guid-2", "Avisa:guid-3"]


def test_what_the_cap_left_behind_is_fetched_by_the_next_run(conn):
    net = Net(
        {FEED.url: rss(item(1), item(2), item(3))}
        | {f"https://avisa.example/sak/{n}": page() for n in (1, 2, 3)}
    )
    clock = Ticking()
    done = [feeds.run([FEED], conn, fetch=net, body_limit=2, clock=clock).bodies for _ in range(3)]
    assert done == [2, 1, 0]


def test_fetch_url_refuses_anything_but_http(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("hemmelig")
    with pytest.raises(ValueError, match="not an http"):
        feeds.fetch_url(secret.as_uri())


# --- retention ------------------------------------------------------------------


@pytest.mark.parametrize(
    "age,published,left",
    (
        (timedelta(days=90) - timedelta(seconds=1), NOW, 1),
        (timedelta(days=90), NOW, 1),
        (timedelta(days=90) + timedelta(seconds=1), NOW, 0),
        # Nothing but the id says an undated row has been seen before.
        (timedelta(days=400), None, 1),
    ),
    ids=["just-inside", "exactly-90-days", "just-outside", "undated"],
)
def test_rows_past_the_radars_own_retention_are_pruned(conn, age, published, left):
    net = Net({FEED.url: rss(item(1, published=published)), "https://avisa.example/sak/1": page()})
    feeds.run([FEED], conn, fetch=net, clock=at(NOW))

    assert feeds.prune(conn, now=NOW + age) == 1 - left
    assert len(rows(conn)) == left


# --- the database file ----------------------------------------------------------


def test_connect_reopens_its_own_database(db):
    feeds.connect(db).close()
    feeds.connect(db).close()


def test_connect_refuses_a_database_it_did_not_create(db):
    """SCRAPER_DB_PATH is also where a real scraper's file lives."""
    other = sqlite3.connect(db)
    other.execute("CREATE TABLE articles (id TEXT PRIMARY KEY, content_hash TEXT)")
    other.commit()
    other.close()

    with pytest.raises(RuntimeError, match="not created by radar-fetch"):
        feeds.connect(db)

    check = sqlite3.connect(db)
    assert [r[1] for r in check.execute("PRAGMA table_info(articles)")] == ["id", "content_hash"]
    assert check.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall() == [
        ("sqlite_autoindex_articles_1",)
    ]
    check.close()


# --- the round trip: what radar-fetch writes, the pipeline reads ---------------


def recent() -> datetime:
    """ScraperSource reads the real clock for its age window, so these do too."""
    return datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)


def test_the_pipeline_reads_back_what_was_fetched(conn, db):
    published = recent()
    net = Net(
        {
            FEED.url: rss(item(1, published=published, summary="<p>En <b>kort</b> ingress</p>")),
            "https://avisa.example/sak/1": page("Rektor"),
        }
    )
    feeds.run([FEED], conn, fetch=net, clock=Ticking(published))

    (article,) = ScraperSource(db).fetch_since("")
    excerpt = article.pop("body_excerpt")
    assert excerpt.startswith("# To skoler legges ned") and "Rektor" in excerpt
    assert len(excerpt) >= 500
    assert article == {
        "id": "Avisa:guid-1",
        "source": "Avisa",
        "feed_name": "Avisa – siste nytt",
        "title": "Tittel 1",
        "link": "https://avisa.example/sak/1",
        "summary": "En kort ingress",
        "published_at": published.isoformat(),
        "published_day": published.astimezone(config.OSLO).date().isoformat(),
        "image_url": None,
        "paywalled": None,
        "scraper_cursor": rows(conn)[0]["body_fetched_at"],
    }
    assert ScraperSource(db).fetch_body("Avisa:guid-1") == rows(conn)[0]["body_markdown"]


def test_an_article_without_a_body_is_not_handed_to_the_pipeline(conn, db):
    net = Net(
        {FEED.url: rss(item(1, published=recent())), "https://avisa.example/sak/1": URLError("x")}
    )
    feeds.run([FEED], conn, fetch=net, clock=Ticking(recent()))
    assert ScraperSource(db).fetch_since("") == []


def test_a_body_that_lands_later_is_past_the_cursor_of_an_earlier_read(conn, db):
    """The silent-loss case: a tick reads between two bodies of the same run.

    Its watermark is the newest stamp it saw. The second body has to carry a
    later one, or the strict `>` of the next read never returns it.
    """
    published = recent()
    net = Net(
        {FEED.url: rss(item(1, published=published), item(2, published=published))}
        | {f"https://avisa.example/sak/{n}": page() for n in (1, 2)}
    )
    clock = Ticking(published)
    feeds.run([FEED], conn, fetch=net, body_limit=1, clock=clock)
    source = ScraperSource(db)
    (first,) = source.fetch_since("")

    feeds.run([FEED], conn, fetch=net, body_limit=1, clock=clock)
    (second,) = source.fetch_since(first["scraper_cursor"])

    assert {first["id"], second["id"]} == {"Avisa:guid-1", "Avisa:guid-2"}
    assert second["scraper_cursor"] > first["scraper_cursor"]


def test_bodies_of_one_run_do_not_share_a_stamp(conn):
    net = Net(
        {FEED.url: rss(item(1), item(2))}
        | {f"https://avisa.example/sak/{n}": page() for n in (1, 2)}
    )
    feeds.run([FEED], conn, fetch=net, clock=Ticking())
    stamps = [r["body_fetched_at"] for r in rows(conn)]
    assert len(set(stamps)) == 2


# --- the command ----------------------------------------------------------------


def test_radar_fetch_without_a_feed_list_says_what_to_do(monkeypatch, tmp_path):
    missing = tmp_path / "feeds.txt"
    monkeypatch.setattr("sys.argv", ["radar-fetch", "--feeds", str(missing)])
    with pytest.raises(SystemExit) as exit_info:
        cli.fetch()
    assert exit_info.value.code == (
        f"no feed list at {missing} — copy feeds.example.txt to feeds.txt"
    )


def test_radar_fetch_exits_1_when_every_feed_failed(monkeypatch, tmp_path, capsys):
    """Port 9 on localhost refuses at once: a real failed fetch, no network."""
    feed_list = tmp_path / "feeds.txt"
    feed_list.write_text("Nede | http://127.0.0.1:9/rss\n", encoding="utf-8")
    monkeypatch.setattr(config, "SCRAPER_DB_PATH", tmp_path / "news_scraper.db")
    monkeypatch.setattr("sys.argv", ["radar-fetch", "--feeds", str(feed_list)])

    with pytest.raises(SystemExit) as exit_info:
        cli.fetch()

    assert exit_info.value.code == 1
    assert capsys.readouterr().out.splitlines()[0] == (
        "feeds=1 feeds_failed=1 entries=0 new_or_changed=0 bodies=0 bodies_failed=0 pruned=0"
    )

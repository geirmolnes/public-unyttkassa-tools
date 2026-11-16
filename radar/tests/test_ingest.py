"""Ingest correctness: the watermark, the model's answers, and retention.

The cursor tests are the ones that matter. Both bugs they pin were silent — the
tick reported a healthy number and the articles were simply never seen again.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from radar import classify, pipeline, storage
from radar.pipeline import _drop_trailing_tie

TODAY = date(2026, 8, 17)


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "radar.db")
    yield connection
    connection.close()


def art(article_id, cursor, *, day=None, link=None):
    return {
        "id": article_id, "source": "VG", "feed_name": None, "title": f"T {article_id}",
        "link": link or f"https://vg.no/{article_id}", "summary": "s",
        "body_excerpt": "Full tekst om skole og lærere i grunnskolen.",
        "published_at": f"{day or TODAY.isoformat()}T08:00:00+00:00",
        "published_day": day or TODAY.isoformat(),
        "image_url": None, "paywalled": 0, "scraper_cursor": cursor,
    }


# --- the tie-group trim -------------------------------------------------------


@pytest.mark.parametrize(
    "cursors,limit,kept",
    (
        # Short of the limit: nothing was truncated, so nothing is dropped.
        (["a", "b", "c"], 5, ["a", "b", "c"]),
        (["a", "b", "c"], 4, ["a", "b", "c"]),
        # Exactly at the limit: the last cursor group may have been cut in half.
        (["a", "b", "c"], 3, ["a", "b"]),
        (["a", "b", "b"], 3, ["a"]),
        (["a", "a", "b"], 3, ["a", "a"]),
        # One row, one group, and it filled the limit — take it or never advance.
        (["a"], 1, ["a"]),
        (["a", "a", "a"], 3, ["a", "a", "a"]),
        ([], 5, []),
    ),
)
def test_drop_trailing_tie(cursors, limit, kept):
    articles = [art(str(i), c) for i, c in enumerate(cursors)]
    assert [a["scraper_cursor"] for a in _drop_trailing_tie(articles, limit)] == kept


class FakeSource:
    """A scraper that hands back a fixed page and records what was asked for."""

    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def initial_cursor(self):
        return ""

    def fetch_since(self, cursor, *, limit):
        self.asked.append(cursor)
        page = self.pages.pop(0) if self.pages else []
        return page[:limit]


def test_cursor_stops_short_of_a_split_tie_group(conn):
    """A LIMIT landing inside one timestamp must not skip the rest of it."""
    source = FakeSource([[art("a", "t1"), art("b", "t2"), art("c", "t2")]])
    ingested, _ = pipeline.ingest(conn, source, limit=3)

    assert ingested == 1
    assert storage.get_state(conn, pipeline.CURSOR_KEY) == "t1"


def test_the_skipped_group_comes_back_on_the_next_tick(conn):
    source = FakeSource(
        [
            [art("a", "t1"), art("b", "t2"), art("c", "t2")],
            [art("b", "t2"), art("c", "t2")],
        ]
    )
    pipeline.ingest(conn, source, limit=3)
    pipeline.ingest(conn, source, limit=3)

    stored = {r[0] for r in conn.execute("SELECT id FROM articles")}
    assert stored == {"a", "b", "c"}
    assert source.asked == ["", "t1"]


def test_a_full_page_of_one_timestamp_still_advances(conn):
    """Otherwise a batch of identical timestamps is an infinite loop."""
    source = FakeSource([[art("a", "t1"), art("b", "t1")]])
    ingested, _ = pipeline.ingest(conn, source, limit=2)

    assert ingested == 2
    assert storage.get_state(conn, pipeline.CURSOR_KEY) == "t1"


def test_nothing_fetched_leaves_the_cursor_alone(conn):
    storage.set_state(conn, pipeline.CURSOR_KEY, "t9")
    assert pipeline.ingest(conn, FakeSource([[]]), limit=10) == (0, 0)
    assert storage.get_state(conn, pipeline.CURSOR_KEY) == "t9"


def test_blocked_classification_is_retained_without_endless_retries(conn, monkeypatch):
    from radar.web import queries

    pipeline.ingest(conn, FakeSource([[art("blocked", "t1"), art("retry", "t2")]]), limit=10)
    calls = []

    def classify_article(article):
        calls.append(article["id"])
        error = ("blocked response: PROHIBITED_CONTENT" if article["id"] == "blocked"
                 else "empty response: finish reasons missing")
        return classify.Verdict(error=error)

    monkeypatch.setattr(classify, "classify", classify_article)
    # The block is not an error: only the transient failure counts.
    assert pipeline.classify_pending(conn) == (0, 0, 1)
    assert pipeline.classify_pending(conn) == (0, 0, 1)
    assert calls.count("blocked") == 1
    assert calls.count("retry") == 2
    assert storage.count_pending_classification(conn) == 1
    row = conn.execute("SELECT * FROM verdicts WHERE article_id = 'blocked'").fetchone()
    assert row["relevant"] is None
    assert row["classified_at"] is None
    assert row["error"] == "blocked response: PROHIBITED_CONTENT"
    saved_article = conn.execute("SELECT * FROM articles WHERE id = 'blocked'").fetchone()
    for field in ("title", "link", "summary", "body_excerpt", "published_at"):
        assert saved_article[field] == art("blocked", "t1")[field]
    monkeypatch.setattr(queries, "_stats_cache", None)
    totals = queries.stats(conn, fresh=True)
    assert totals["blocked"] == 1
    assert totals["pending"] == 1

    # A newly ingested version can be classified again.
    pipeline.ingest(conn, FakeSource([[art("blocked", "t3")]]), limit=10)
    assert storage.count_pending_classification(conn) == 2


# --- items anchored on a shared page ------------------------------------------

# An NRK kortnytt: the path is the nyhetssenter page every item in the region
# shares, the fragment is the item. The fetched body is whichever item led the
# page, so storing one files a real headline over someone else's article.
ANCHOR = (
    "https://www.nrk.no/nordland/nyhetssenter-nordland-1.15898380"
    "#1.17992892.1.17992892"
)


def test_an_anchored_item_is_not_stored(conn):
    source = FakeSource([[art("a", "t1"), art("b", "t2", link=ANCHOR)]])
    ingested, _ = pipeline.ingest(conn, source, limit=10)

    assert ingested == 1
    assert [r[0] for r in conn.execute("SELECT id FROM articles")] == ["a"]


STUDIO = "https://www.dagbladet.no/studio/92397-kong-harald-er-død/12403286-møtte-opp-06"


def test_a_dagbladet_studio_post_is_not_stored(conn):
    """The body is the live blog's shell, never the post — see dagbladet.py."""
    studio = {**art("b", "t2", link=STUDIO), "source": "Dagbladet"}
    source = FakeSource([[art("a", "t1"), studio]])
    ingested, _ = pipeline.ingest(conn, source, limit=10)

    assert ingested == 1
    assert [r[0] for r in conn.execute("SELECT id FROM articles")] == ["a"]
    assert storage.get_state(conn, pipeline.CURSOR_KEY) == "t2"


def test_another_outlet_under_a_studio_path_is_stored(conn):
    source = FakeSource([[art("a", "t1", link="https://www.vg.no/studio/1/2")]])
    assert pipeline.ingest(conn, source, limit=10)[0] == 1


def test_prune_clears_studio_posts_that_got_in_before_the_check(conn):
    """Whatever its age: the article from today goes, the one beside it stays."""
    studio = {**art("s", "t1", link=STUDIO), "source": "Dagbladet"}
    for a in (art("a", "t1"), studio):
        storage.upsert_article(conn, a)
    conn.commit()
    storage.prune(conn, days=3650)
    assert [r[0] for r in conn.execute("SELECT id FROM articles ORDER BY id")] == ["a"]


# --- one row per link ---------------------------------------------------------


def test_a_reminted_id_for_a_link_we_hold_updates_that_row(conn):
    """Dagbladet's front page: three ids, three headlines, one article."""
    first = {**art("h1", "t1", link="https://www.dagbladet.no/nyheter/fly/85116750"),
             "title": "Krasjet: Alle avganger stanset"}
    second = {**art("h2", "t2", link="https://www.dagbladet.no/nyheter/fly/85116750"),
              "title": "Minst fem døde", "body_excerpt": "Oppdatert tekst om skole og lærere."}
    storage.record_claim(conn, "h1", "u1", "u1@x")
    ingested, _ = pipeline.ingest(conn, FakeSource([[first, second]]), limit=10)

    rows = conn.execute("SELECT id, title, body_excerpt FROM articles").fetchall()
    assert ingested == 2
    assert [tuple(r) for r in rows] == [("h1", "Minst fem døde", "Oppdatert tekst om skole og lærere.")]
    assert conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 1
    assert storage.claims_for(conn, ["h1"])["h1"][0]["user_id"] == "u1"


def test_the_same_link_at_another_source_is_another_row(conn):
    a = art("h1", "t1", link="https://www.nrk.no/x-1.1")
    b = {**art("h2", "t2", link="https://www.nrk.no/x-1.1"), "source": "Nettavisen"}
    pipeline.ingest(conn, FakeSource([[a, b]]), limit=10)
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 2


def test_prune_collapses_duplicate_links_keeping_the_newest(conn):
    link = "https://www.dagbladet.no/nyheter/fly/85116750"
    for aid, stamp in (("old", "2026-08-01T00:00:00"), ("new", "2026-08-02T00:00:00"),
                       ("mid", "2026-08-01T12:00:00")):
        storage.upsert_article(conn, art(aid, "t", link=link))
        conn.execute("UPDATE articles SET ingested_at = ? WHERE id = ?", (stamp, aid))
    conn.commit()
    storage.prune(conn, days=3650)
    assert [r[0] for r in conn.execute("SELECT id FROM articles")] == ["new"]


def test_prune_keeps_a_cited_duplicate(conn):
    link = "https://www.dagbladet.no/nyheter/fly/85116750"
    for aid, stamp in (("old", "2026-08-01T00:00:00"), ("new", "2026-08-02T00:00:00")):
        storage.upsert_article(conn, art(aid, "t", link=link))
        conn.execute("UPDATE articles SET ingested_at = ? WHERE id = ?", (stamp, aid))
    conn.execute(
        "INSERT INTO coverage (article_id, status, method, checked_at) VALUES ('old', 'sitert', 'link', 'x')"
    )
    conn.commit()
    storage.prune(conn, days=3650)
    assert sorted(r[0] for r in conn.execute("SELECT id FROM articles")) == ["new", "old"]


def test_prune_clears_pinned_live_entries_that_got_in_before_the_check(conn):
    pinned = art("p", "t1", link="https://www.bt.no/nyheter/direkte/i/BR649v/x?pinnedEntry=1")
    for a in (art("a", "t1"), pinned):
        storage.upsert_article(conn, a)
    conn.commit()
    storage.prune(conn, days=3650)
    assert [r[0] for r in conn.execute("SELECT id FROM articles")] == ["a"]


def test_the_cursor_clears_a_batch_that_is_all_anchors(conn):
    """The one way this could stall: nothing stored, so nothing to read it from.

    The watermark comes off the batch as fetched, not off what survived the
    filter — otherwise a tick that drops every row leaves the cursor where it
    was and re-reads the same rows on every tick, for ever.
    """
    source = FakeSource([[art("a", "t1", link=ANCHOR), art("b", "t2", link=ANCHOR)]])
    ingested, passed = pipeline.ingest(conn, source, limit=10)

    assert (ingested, passed) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0
    assert storage.get_state(conn, pipeline.CURSOR_KEY) == "t2"


def test_the_watermark_counts_the_anchor_that_was_dropped(conn):
    """Taken from the last row fetched, even when that row never got stored."""
    source = FakeSource([[art("a", "t1"), art("b", "t2", link=ANCHOR)]])
    pipeline.ingest(conn, source, limit=10)

    assert storage.get_state(conn, pipeline.CURSOR_KEY) == "t2"


# --- what the classifier is allowed to store ----------------------------------


def verdict_for(monkeypatch, payload):
    monkeypatch.setattr(classify.gemini, "generate_json", lambda *a, **k: payload)
    return classify.classify({"source": "VG", "title": "T", "summary": "", "body_excerpt": ""})


@pytest.mark.parametrize(
    "score,expected",
    (
        (-1, 0),
        (0, 0),
        (1, 1),
        (99, 99),
        (100, 100),
        (101, 100),
        (10_000, 100),
        # A relevant article with no usable score used to store NULL, and every
        # feed query compares `v.score >= ?` — so the row vanished from the feed
        # and from every count, with no error anywhere.
        (None, 0),
        ("høy", 0),
        (True, 0),
    ),
)
def test_score_is_clamped_and_never_null_for_a_relevant_article(monkeypatch, score, expected):
    v = verdict_for(monkeypatch, {"relevant": True, "score": score, "stage": "barnehage",
                                  "kind": "nyhet", "topics": [], "rationale": "r"})
    assert v.score == expected


def test_an_irrelevant_article_may_have_no_score(monkeypatch):
    v = verdict_for(monkeypatch, {"relevant": False, "score": None, "stage": "barnehage",
                                  "kind": "nyhet", "topics": [], "rationale": "r"})
    assert v.score is None


@pytest.mark.parametrize(
    "stage,expected",
    (
        ("barnehage", "barnehage"),
        ("tverrgaende", "tverrgaende"),
        # In STAGES but not in STAGE_LABELS: it would render its raw key in the
        # rail and be counted under a heading nobody can click.
        ("ikke_relevant", "tverrgaende"),
        ("universitet", "tverrgaende"),
        (None, "tverrgaende"),
        (17, "tverrgaende"),
    ),
)
def test_stage_falls_back_to_a_label_that_exists(monkeypatch, stage, expected):
    v = verdict_for(monkeypatch, {"relevant": True, "score": 50, "stage": stage,
                                  "kind": "nyhet", "topics": [], "rationale": "r"})
    assert v.stage == expected


@pytest.mark.parametrize(
    "kind,expected",
    (("debatt", "debatt"), ("leder", "nyhet"), (None, "nyhet"), ("", "nyhet")),
)
def test_kind_falls_back_to_nyhet(monkeypatch, kind, expected):
    v = verdict_for(monkeypatch, {"relevant": True, "score": 50, "stage": "barnehage",
                                  "kind": kind, "topics": [], "rationale": "r"})
    assert v.kind == expected


@pytest.mark.parametrize(
    "topics,expected",
    (
        (["lønn", "streik"], ["lønn", "streik"]),
        (["lønn", "", "  "], ["lønn"]),
        ([], []),
        (None, []),
    ),
)
def test_topics_drop_blanks(monkeypatch, topics, expected):
    v = verdict_for(monkeypatch, {"relevant": True, "score": 50, "stage": "barnehage",
                                  "kind": "nyhet", "topics": topics, "rationale": "r"})
    assert v.topics == expected


# --- the tick lock ------------------------------------------------------------


def test_the_lock_is_held_by_one_caller(conn):
    assert storage.acquire_tick_lock(conn) is True
    assert storage.acquire_tick_lock(conn) is False


def test_releasing_the_lock_lets_the_next_tick_in(conn):
    storage.acquire_tick_lock(conn)
    storage.release_tick_lock(conn)
    assert storage.acquire_tick_lock(conn) is True


def held_for(minutes: float) -> str:
    """A lock stamp that many minutes in the past."""
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


STALE_MINUTES = storage.TICK_LOCK_STALE_HOURS * 60


def test_a_stale_lock_is_broken(conn):
    """A container killed mid-tick must not block the schedule forever."""
    storage.set_state(conn, storage.TICK_LOCK_KEY, held_for(STALE_MINUTES + 1))
    assert storage.acquire_tick_lock(conn) is True


def test_a_fresh_lock_is_not_broken(conn):
    """A minute short of the window is a tick still running, not a corpse."""
    storage.set_state(conn, storage.TICK_LOCK_KEY, held_for(STALE_MINUTES - 1))
    assert storage.acquire_tick_lock(conn) is False


# --- retention ----------------------------------------------------------------


def seed_for_prune(conn, article_id, *, days_old, cited=False):
    day = (TODAY - timedelta(days=days_old)).isoformat()
    storage.upsert_article(conn, art(article_id, "c", day=day))
    conn.execute(
        "INSERT INTO verdicts (article_id, prefilter_passed, relevant, score) VALUES (?,1,1,70)",
        (article_id,),
    )
    if cited:
        conn.execute(
            "INSERT INTO coverage (article_id, status, checks) VALUES (?, 'sitert', 0)",
            (article_id,),
        )
    conn.commit()


@pytest.mark.parametrize(
    "days_old,cited,survives",
    (
        (89, False, True),    # one inside the window
        (90, False, True),    # exactly at it — the cutoff is a strict <
        (91, False, False),   # one outside
        (365, False, False),
        # Proof of a citation is the one answer in here that took reporting to
        # establish. It costs a row, so it is kept whatever its age.
        (365, True, True),
    ),
)
def test_prune_boundaries(conn, monkeypatch, days_old, cited, survives):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 17, 12, 0, tzinfo=tz or UTC)

    monkeypatch.setattr(storage, "datetime", Frozen)
    seed_for_prune(conn, "a", days_old=days_old, cited=cited)
    storage.prune(conn, days=90)

    remaining = [r[0] for r in conn.execute("SELECT id FROM articles")]
    assert remaining == (["a"] if survives else [])


def test_prune_cascades_to_the_verdict(conn, monkeypatch):
    """ON DELETE CASCADE is inert unless the connection turns foreign keys on."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 17, 12, 0, tzinfo=tz or UTC)

    monkeypatch.setattr(storage, "datetime", Frozen)
    seed_for_prune(conn, "a", days_old=200)
    storage.prune(conn, days=90)

    assert conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0


# --- what counts as a failed tick ---------------------------------------------


@pytest.mark.parametrize(
    "errors,classified,clustered,covered,exits",
    (
        # Nothing wrong.
        (0, 10, 5, 2, False),
        (0, 0, 0, 0, False),      # a quiet tick with nothing to do
        # Some calls flaked but work got through — they retry next tick.
        (1, 10, 5, 2, False),
        (1, 1, 0, 0, False),
        (9, 1, 0, 0, False),
        # Everything failed: a wrong key, a dead API, a bad prompt version.
        (1, 0, 0, 0, True),
        (50, 0, 0, 0, True),
    ),
)
def test_only_a_wholly_failed_tick_is_a_failure(errors, classified, clustered, covered, exits):
    result = pipeline.TickResult(
        errors=errors, classified=classified, clustered=clustered, covered=covered
    )
    assert result.achieved_nothing is exits


def test_a_tick_whose_only_article_is_blocked_is_not_a_failure(conn, monkeypatch):
    """Regression, 2026-11-11: one PROHIBITED_CONTENT verdict paged as FAIL."""
    pipeline.ingest(conn, FakeSource([[art("blocked", "t1")]]), limit=10)
    monkeypatch.setattr(
        classify, "classify",
        lambda article: classify.Verdict(error="blocked response: PROHIBITED_CONTENT"),
    )
    classified, _, errors = pipeline.classify_pending(conn)
    assert (classified, errors) == (0, 0)
    assert pipeline.TickResult(classified=classified, errors=errors).achieved_nothing is False


def test_a_cited_only_tick_counts_as_work():
    """The link check is free and runs every tick; it succeeding is a success."""
    assert pipeline.TickResult(errors=3, cited=1).achieved_nothing is False

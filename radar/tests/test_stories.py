"""Running stories in the database: migration, span bookkeeping, and what the
feed and story page read back out."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from radar import pipeline, storage
from radar.cluster import Assignment
from radar.web import queries
from radar.web.searchq import parse as parse_q

TODAY = date(2026, 8, 6)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Pin "now" so the candidate window's edges are exact, not midnight-dependent."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 6, 12, 0, tzinfo=tz or UTC)

    monkeypatch.setattr(storage, "datetime", Frozen)


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "radar.db")
    yield connection
    connection.close()


def day(offset: int) -> str:
    """An ISO day `offset` days before the frozen today."""
    return (TODAY - timedelta(days=offset)).isoformat()


def add_article(
    conn, article_id, *, source, day, title="Tittel", score=70, relevant=1, topics=None,
    time="08:00:00",
):
    conn.execute(
        """
        INSERT INTO articles (id, source, title, link, published_at, published_day,
                              scraper_cursor, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, '', '')
        """,
        (article_id, source, title, f"https://{source.lower()}.no/{article_id}",
         f"{day}T{time}+00:00", day),
    )
    conn.execute(
        """
        INSERT INTO verdicts (article_id, prefilter_passed, relevant, score, topics,
                              classified_at)
        VALUES (?, 1, ?, ?, ?, 'stamped')
        """,
        (article_id, relevant, score, json.dumps(topics) if topics else None),
    )
    conn.commit()


# --- schema and migration -----------------------------------------------------


def test_fresh_database_has_the_cluster_column(conn):
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(verdicts)")}
    assert "cluster_id" in columns


def test_existing_database_gains_the_cluster_column(tmp_path):
    """The deployed DB predates clustering — connect() must migrate it in place."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE verdicts (
            article_id TEXT PRIMARY KEY,
            prefilter_passed INTEGER NOT NULL,
            prefilter_reason TEXT, prefilter_terms TEXT,
            relevant INTEGER, score INTEGER, stage TEXT, topics TEXT,
            rationale TEXT, model TEXT, prompt_version TEXT,
            classified_at TEXT, error TEXT
        );
        """
    )
    old.commit()
    old.close()

    migrated = storage.connect(path)
    try:
        columns = {r["name"] for r in migrated.execute("PRAGMA table_info(verdicts)")}
        indexes = {r["name"] for r in migrated.execute("PRAGMA index_list(verdicts)")}
        assert "cluster_id" in columns
        assert "idx_verdicts_cluster" in indexes
    finally:
        migrated.close()


def test_existing_tick_runs_gains_the_cluster_counters(tmp_path):
    """The deployed tick_runs has rows and no cluster columns; a tick must still log."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE tick_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL, finished_at TEXT,
            ingested INTEGER, prefiltered_in INTEGER, classified INTEGER,
            relevant INTEGER, errors INTEGER, error TEXT
        );
        INSERT INTO tick_runs (started_at) VALUES ('before-the-migration');
        """
    )
    old.commit()
    old.close()

    migrated = storage.connect(path)
    try:
        run_id = storage.log_tick_start(migrated)
        storage.log_tick_finish(migrated, run_id, clustered=4, new_stories=2, relevant=9)
        row = migrated.execute(
            "SELECT clustered, new_stories FROM tick_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert (row["clustered"], row["new_stories"]) == (4, 2)
        assert migrated.execute("SELECT COUNT(*) FROM tick_runs").fetchone()[0] == 2
    finally:
        migrated.close()


def test_connect_is_idempotent(tmp_path):
    """Every tick reconnects; the migration must not fail on the second pass."""
    path = tmp_path / "twice.db"
    for _ in range(2):
        c = storage.connect(path)
        c.close()


# --- the work queue -----------------------------------------------------------


def test_pending_clustering_takes_only_unclustered_relevant_articles(conn):
    add_article(conn, "a", source="NRK", day=day(0))
    add_article(conn, "b", source="VG", day=day(0), relevant=0)
    add_article(conn, "c", source="Khrono", day=day(0))
    cluster_id = storage.create_cluster(conn, "Alt om noe", day(0))
    storage.attach_to_cluster(conn, "c", cluster_id, day(0))
    conn.commit()

    assert [r["id"] for r in storage.pending_clustering(conn)] == ["a"]


def test_pending_clustering_is_oldest_first(conn):
    """The article that broke a story should found it, not the one that followed."""
    add_article(conn, "new", source="VG", day=day(0))
    add_article(conn, "old", source="NRK", day=day(3))
    assert [r["id"] for r in storage.pending_clustering(conn)] == ["old", "new"]


@pytest.mark.parametrize(
    "limit,expected",
    ((2, ["a0", "a1"]), (0, ["a0", "a1", "a2"]), (None, ["a0", "a1", "a2"])),
    ids=["capped", "zero-drains-everything", "none-drains-everything"],
)
def test_pending_clustering_limit(conn, limit, expected):
    for i in range(3):
        add_article(conn, f"a{i}", source="NRK", day=day(3 - i))
    assert [r["id"] for r in storage.pending_clustering(conn, limit=limit)] == expected


def test_pending_clustering_parses_topics(conn):
    add_article(conn, "a", source="NRK", day=day(0), topics=["mobil", "skole"])
    add_article(conn, "b", source="VG", day=day(0))
    queue = {r["id"]: r["topics"] for r in storage.pending_clustering(conn)}
    assert queue == {"a": ["mobil", "skole"], "b": []}


def test_count_pending_clustering(conn):
    add_article(conn, "a", source="NRK", day=day(0))
    add_article(conn, "b", source="VG", day=day(0))
    add_article(conn, "c", source="Khrono", day=day(0), relevant=0)
    cluster_of(conn, "Sak", day(0), ["b"])
    assert storage.count_pending_clustering(conn) == 1


# --- the candidate window -----------------------------------------------------


@pytest.mark.parametrize(
    "age_days,expected",
    ((29, 1), (30, 1), (31, 0)),
    ids=["inside", "exactly-at-cutoff", "one-day-past"],
)
def test_open_clusters_window_is_inclusive_at_the_cutoff(conn, age_days, expected):
    add_article(conn, "a", source="NRK", day=day(age_days))
    cluster_id = storage.create_cluster(conn, "Gammel sak", day(age_days))
    storage.attach_to_cluster(conn, "a", cluster_id, day(age_days))
    conn.commit()

    assert len(storage.open_clusters(conn, days=30)) == expected


def test_open_clusters_is_uncapped_and_freshest_first(conn):
    """Trimming is shortlist's job — cutting here by recency would hide exactly
    the quiet story that topic ranking exists to bring back."""
    for i in range(5):
        add_article(conn, f"a{i}", source="NRK", day=day(i))
        cluster_id = storage.create_cluster(conn, f"Sak {i}", day(i))
        storage.attach_to_cluster(conn, f"a{i}", cluster_id, day(i))
    conn.commit()

    labels = [c["label"] for c in storage.open_clusters(conn, days=45)]
    assert labels == ["Sak 0", "Sak 1", "Sak 2", "Sak 3", "Sak 4"]


def test_open_clusters_carries_span_and_counts(conn):
    cluster_id = storage.create_cluster(conn, "Mobilforbud", day(5))
    for i, (article_id, source, offset) in enumerate(
        (("a", "NRK", 5), ("b", "VG", 2), ("c", "Khrono", 0))
    ):
        add_article(conn, article_id, source=source, day=day(offset), title=f"Sak {i}")
        storage.attach_to_cluster(conn, article_id, cluster_id, day(offset))
    conn.commit()

    (candidate,) = storage.open_clusters(conn, days=45)
    assert candidate["first_day"] == day(5)
    assert candidate["last_day"] == day(0)
    assert candidate["n_articles"] == 3
    assert candidate["titles"] == ["Sak 0", "Sak 1", "Sak 2"]


@pytest.mark.parametrize(
    "n_articles,expected",
    (
        (5, ["Sak 0", "Sak 1", "Sak 2", "Sak 3", "Sak 4"]),  # exactly at the cap
        (6, ["Sak 0", "Sak 2", "Sak 3", "Sak 4", "Sak 5"]),  # one above: the middle goes
        (9, ["Sak 0", "Sak 5", "Sak 6", "Sak 7", "Sak 8"]),
    ),
)
def test_open_clusters_keeps_the_founding_title_and_the_latest(conn, n_articles, expected):
    """A story is judged by its titles, so the founding one has to survive the
    cut — it is what says which event the story is actually about."""
    cluster_id = storage.create_cluster(conn, "Mobilforbud", day(0))
    for i in range(n_articles):
        add_article(conn, f"a{i}", source="NRK", day=day(0), title=f"Sak {i}", time=f"08:{i:02}:00")
        storage.attach_to_cluster(conn, f"a{i}", cluster_id, day(0))
    conn.commit()

    (candidate,) = storage.open_clusters(conn, days=45)
    assert candidate["titles"] == expected


def test_open_clusters_gives_one_title_for_a_story_of_one(conn):
    add_article(conn, "a", source="NRK", day=day(0), title="Alene")
    cluster_id = storage.create_cluster(conn, "Sak", day(0))
    storage.attach_to_cluster(conn, "a", cluster_id, day(0))
    conn.commit()

    (candidate,) = storage.open_clusters(conn, days=45)
    assert candidate["titles"] == ["Alene"]


def test_open_clusters_unions_the_topics_of_its_articles(conn):
    """The union is what shortlist ranks against."""
    cluster_id = storage.create_cluster(conn, "Mobilforbud", day(2))
    add_article(conn, "a", source="NRK", day=day(2), topics=["mobil", "skole"])
    add_article(conn, "b", source="VG", day=day(0), topics=["skole", "vedtak"])
    for article_id, offset in (("a", 2), ("b", 0)):
        storage.attach_to_cluster(conn, article_id, cluster_id, day(offset))
    conn.commit()

    (candidate,) = storage.open_clusters(conn, days=45)
    assert candidate["topics"] == ["mobil", "skole", "vedtak"]


def test_open_clusters_ignores_a_cluster_with_no_articles(conn):
    storage.create_cluster(conn, "Tom sak", day(0))
    conn.commit()
    assert storage.open_clusters(conn, days=45) == []


# --- rebuilding the stories ---------------------------------------------------


def test_clearing_puts_every_article_back_in_the_queue(conn):
    add_article(conn, "a", source="NRK", day=day(1))
    add_article(conn, "b", source="VG", day=day(0))
    cluster_of(conn, "Kunnskapsløft og skolepolitikk", day(0), ["a", "b"])

    assert storage.clear_cluster_assignments(conn) == (2, 1)
    assert storage.open_clusters(conn, days=45) == []
    assert [a["id"] for a in storage.pending_clustering(conn)] == ["a", "b"]


def test_clearing_since_leaves_the_older_story_standing(conn):
    add_article(conn, "old", source="NRK", day=day(9))
    add_article(conn, "new", source="VG", day=day(0))
    kept = cluster_of(conn, "Gammel sak", day(9), ["old"])
    cluster_of(conn, "Fersk sak", day(0), ["new"])

    assert storage.clear_cluster_assignments(conn, since=day(3)) == (1, 1)
    assert [c["id"] for c in storage.open_clusters(conn, days=45)] == [kept]
    assert [a["id"] for a in storage.pending_clustering(conn)] == ["new"]


# --- span bookkeeping ---------------------------------------------------------


@pytest.mark.parametrize(
    "attach_day,expected_span",
    (
        (day(9), (day(9), day(5))),  # earlier than first_day widens backwards
        (day(1), (day(5), day(1))),  # later than last_day widens forwards
        (day(5), (day(5), day(5))),  # the founding day itself moves nothing
    ),
    ids=["earlier", "later", "same"],
)
def test_attaching_widens_the_span(conn, attach_day, expected_span):
    cluster_id = storage.create_cluster(conn, "Sak", day(5))
    add_article(conn, "a", source="NRK", day=attach_day)
    storage.attach_to_cluster(conn, "a", cluster_id, attach_day)
    conn.commit()

    row = conn.execute("SELECT first_day, last_day FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
    assert (row["first_day"], row["last_day"]) == expected_span


def test_reclassifying_releases_the_article(conn):
    """A re-scraped article is re-gated, so its old story must not linger."""
    from radar.prefilter import PrefilterResult

    add_article(conn, "a", source="NRK", day=day(0))
    cluster_id = storage.create_cluster(conn, "Sak", day(0))
    storage.attach_to_cluster(conn, "a", cluster_id, day(0))
    conn.commit()

    storage.record_prefilter(conn, "a", PrefilterResult(passed=True, reason="x"))
    conn.commit()

    row = conn.execute("SELECT cluster_id FROM verdicts WHERE article_id = 'a'").fetchone()
    assert row["cluster_id"] is None


# --- what the feed shows ------------------------------------------------------


def cluster_of(conn, label, day_, members):
    cluster_id = storage.create_cluster(conn, label, day_)
    for article_id in members:
        storage.attach_to_cluster(conn, article_id, cluster_id, day_)
    conn.commit()
    return cluster_id


def other_sources_for(conn, article_id):
    page = queries.feed(conn, parse_q(""))
    for _, items in page.days:
        for item in items:
            if item["id"] == article_id:
                return item["other_sources"]
    raise AssertionError(f"{article_id} not in feed")


def test_unclustered_article_counts_no_other_sources(conn):
    add_article(conn, "a", source="NRK", day=day(0))
    assert other_sources_for(conn, "a") == 0


def test_a_story_of_one_counts_no_other_sources(conn):
    add_article(conn, "a", source="NRK", day=day(0))
    cluster_of(conn, "Sak", day(0), ["a"])
    assert other_sources_for(conn, "a") == 0


def test_two_outlets_count_one_other_source(conn):
    """The smallest case that renders a badge, and the one the singular is for."""
    add_article(conn, "a", source="NRK", day=day(0))
    add_article(conn, "b", source="VG", day=day(0))
    cluster_of(conn, "Sak", day(0), ["a", "b"])
    assert other_sources_for(conn, "a") == 1


def test_the_same_outlet_twice_counts_once(conn):
    """NRK filing a follow-up is not a second source."""
    add_article(conn, "a", source="NRK", day=day(0))
    add_article(conn, "b", source="NRK", day=day(0))
    add_article(conn, "c", source="VG", day=day(0))
    cluster_of(conn, "Sak", day(0), ["a", "b", "c"])
    assert other_sources_for(conn, "a") == 1


def test_joined_coverage_counts_as_another_source(conn):
    """An article the classifier turned down still covered the event."""
    add_article(conn, "a", source="NRK", day=day(0))
    add_article(conn, "b", source="VG", day=day(0), relevant=0)
    cluster_of(conn, "Sak", day(0), ["a", "b"])
    assert other_sources_for(conn, "a") == 1


def test_an_irrelevant_article_outside_the_story_is_not_counted(conn):
    """Only coverage that actually joined counts — not everything scored 0."""
    add_article(conn, "a", source="NRK", day=day(0))
    add_article(conn, "b", source="VG", day=day(0), relevant=0)
    cluster_of(conn, "Sak", day(0), ["a"])
    assert other_sources_for(conn, "a") == 0


# --- the timeline on the story page -------------------------------------------


def test_timeline_is_empty_without_a_story(conn):
    add_article(conn, "a", source="NRK", day=day(0))
    item = queries.get_article(conn, "a")
    assert queries.story_timeline(conn, item) == []


def test_timeline_is_empty_for_a_story_of_one(conn):
    """One article is not a forløp — showing a one-node timeline is noise."""
    add_article(conn, "a", source="NRK", day=day(0))
    cluster_of(conn, "Sak", day(0), ["a"])
    item = queries.get_article(conn, "a")
    assert queries.story_timeline(conn, item) == []


def test_timeline_appears_at_two_articles(conn):
    add_article(conn, "a", source="NRK", day=day(0))
    add_article(conn, "b", source="VG", day=day(0))
    cluster_of(conn, "Sak", day(0), ["a", "b"])

    timeline = queries.story_timeline(conn, queries.get_article(conn, "a"))
    assert len(timeline) == 1
    assert [a["source"] for a in timeline[0]["articles"]] == ["NRK", "VG"]


def test_timeline_groups_by_day_newest_first(conn):
    cluster_id = storage.create_cluster(conn, "Mobilforbud", day(4))
    for article_id, source, offset in (("a", "NRK", 4), ("b", "VG", 0), ("c", "Khrono", 0)):
        add_article(conn, article_id, source=source, day=day(offset))
        storage.attach_to_cluster(conn, article_id, cluster_id, day(offset))
    conn.commit()

    timeline = queries.story_timeline(conn, queries.get_article(conn, "b"))
    assert [beat["day"] for beat in timeline] == [day(0), day(4)]
    assert [a["source"] for a in timeline[0]["articles"]] == ["VG", "Khrono"]


def test_timeline_runs_backwards_inside_a_day(conn):
    """The latest beat is the point of the card, whatever the articles scored."""
    cluster_id = storage.create_cluster(conn, "Mobilforbud", day(0))
    for article_id, source, time, score in (
        ("a", "NRK", "07:20:00", 90),
        ("b", "VG", "11:50:00", 40),
        ("c", "Khrono", "21:09:00", 70),
    ):
        add_article(conn, article_id, source=source, day=day(0), time=time, score=score)
        storage.attach_to_cluster(conn, article_id, cluster_id, day(0))
    conn.commit()

    timeline = queries.story_timeline(conn, queries.get_article(conn, "a"))
    assert [a["source"] for a in timeline[0]["articles"]] == ["Khrono", "VG", "NRK"]


def test_timeline_marks_the_article_being_read(conn):
    add_article(conn, "a", source="NRK", day=day(0))
    add_article(conn, "b", source="VG", day=day(0))
    cluster_of(conn, "Sak", day(0), ["a", "b"])

    timeline = queries.story_timeline(conn, queries.get_article(conn, "b"))
    marked = [a["source"] for a in timeline[0]["articles"] if a["current"]]
    assert marked == ["VG"]


def test_timeline_leaves_out_coverage_the_classifier_turned_down(conn):
    """A story left over from when turned-down articles could join must not
    reach the page — the query is the guarantee, not the pipeline."""
    add_article(conn, "stale", source="NRK", day=day(1), relevant=0)
    add_article(conn, "a", source="Aftenposten", day=day(0))
    add_article(conn, "b", source="VG", day=day(0))
    cluster_of(conn, "Sak", day(0), ["stale", "a", "b"])

    timeline = queries.story_timeline(conn, queries.get_article(conn, "a"))
    assert [b["day"] for b in timeline] == [day(0)]
    assert [x["source"] for x in timeline[0]["articles"]] == ["Aftenposten", "VG"]


def test_timeline_ignores_irrelevant_articles_that_never_joined(conn):
    add_article(conn, "a", source="NRK", day=day(0))
    add_article(conn, "loose", source="VG", day=day(0), relevant=0)
    cluster_of(conn, "Sak", day(0), ["a"])

    assert queries.story_timeline(conn, queries.get_article(conn, "a")) == []


def test_story_page_carries_the_label(conn):
    add_article(conn, "a", source="NRK", day=day(0))
    cluster_of(conn, "Mobilforbud i grunnskolen", day(0), ["a"])
    assert queries.get_article(conn, "a")["cluster_label"] == "Mobilforbud i grunnskolen"


# --- the tick step that drives it ---------------------------------------------


def answers(monkeypatch, replies):
    """Stub cluster.assign with one reply per call, and record what it was shown."""
    seen = []
    replies = list(replies)

    def fake_assign(article, candidates):
        seen.append({"id": article["id"], "candidates": [c["label"] for c in candidates]})
        return replies.pop(0)

    monkeypatch.setattr(pipeline.cluster, "assign", fake_assign)
    return seen


def labels_of(conn):
    rows = conn.execute(
        """
        SELECT v.article_id, c.label
          FROM verdicts v LEFT JOIN clusters c ON c.id = v.cluster_id
         ORDER BY v.article_id
        """
    ).fetchall()
    return {r["article_id"]: r["label"] for r in rows}


def test_cluster_pending_founds_a_story(conn, monkeypatch):
    add_article(conn, "a", source="NRK", day=day(0))
    answers(monkeypatch, [Assignment(handle="NY", label="Mobilforbud")])

    assert pipeline.cluster_pending(conn) == (1, 1, 0)
    assert labels_of(conn) == {"a": "Mobilforbud"}


@pytest.mark.parametrize(
    "handle,expected",
    (("S1", "Første sak"), ("S2", "Andre sak")),
    ids=["first-handle", "last-handle"],
)
def test_cluster_pending_maps_the_handle_to_the_right_story(conn, monkeypatch, handle, expected):
    """S1 must be candidates[0] — an off-by-one here files stories under the wrong label."""
    for i, label in enumerate(("Første sak", "Andre sak")):
        add_article(conn, f"seed{i}", source="NRK", day=day(i))
        cluster_of(conn, label, day(i), [f"seed{i}"])
    add_article(conn, "new", source="VG", day=day(0))

    seen = answers(monkeypatch, [Assignment(handle=handle, label=expected)])
    assert pipeline.cluster_pending(conn) == (1, 0, 0)
    assert seen[0]["candidates"] == ["Første sak", "Andre sak"]  # freshest first
    assert labels_of(conn)["new"] == expected


def test_a_founded_story_is_offered_to_the_next_article(conn, monkeypatch):
    """The whole reason clustering is sequential: a morning's coverage must be
    able to join the story the first article of that morning created."""
    add_article(conn, "first", source="NRK", day=day(0))
    add_article(conn, "second", source="VG", day=day(0))
    seen = answers(
        monkeypatch,
        [Assignment(handle="NY", label="Mobilforbud"), Assignment(handle="S1", label="Mobilforbud")],
    )

    assert pipeline.cluster_pending(conn) == (2, 1, 0)
    assert seen[0]["candidates"] == []
    assert seen[1]["candidates"] == ["Mobilforbud"]
    assert labels_of(conn) == {"first": "Mobilforbud", "second": "Mobilforbud"}


def test_a_failed_assignment_leaves_the_article_for_the_next_tick(conn, monkeypatch):
    add_article(conn, "a", source="NRK", day=day(0))
    answers(monkeypatch, [Assignment(error="429 rate limited")])

    assert pipeline.cluster_pending(conn) == (0, 0, 1)
    assert labels_of(conn) == {"a": None}
    assert [r["id"] for r in storage.pending_clustering(conn)] == ["a"]


def test_cluster_pending_does_nothing_with_an_empty_queue(conn, monkeypatch):
    monkeypatch.setattr(
        pipeline.cluster, "assign", lambda *a: pytest.fail("should not call the model")
    )
    assert pipeline.cluster_pending(conn) == (0, 0, 0)


def test_cluster_pending_stops_at_the_tick_cap(conn, monkeypatch):
    """One tick must not turn a backlog into an unbounded run of LLM calls."""
    monkeypatch.setattr(pipeline.config, "CLUSTER_LIMIT", 2)
    for i in range(5):
        add_article(conn, f"a{i}", source="NRK", day=day(5 - i))
    seen = answers(monkeypatch, [Assignment(handle="NY", label=f"Sak {i}") for i in range(5)])

    assert pipeline.cluster_pending(conn) == (2, 2, 0)
    assert [s["id"] for s in seen] == ["a0", "a1"]  # oldest first, rest next tick
    assert storage.count_pending_clustering(conn) == 3


def test_cluster_pending_zero_drains_the_backlog(conn, monkeypatch):
    monkeypatch.setattr(pipeline.config, "CLUSTER_LIMIT", 2)
    for i in range(4):
        add_article(conn, f"a{i}", source="NRK", day=day(4 - i))
    answers(monkeypatch, [Assignment(handle="NY", label=f"Sak {i}") for i in range(4)])

    assert pipeline.cluster_pending(conn, limit=0) == (4, 4, 0)
    assert storage.count_pending_clustering(conn) == 0


def test_cluster_pending_shows_the_model_a_shortlist(conn, monkeypatch):
    """The prompt gets CLUSTER_CANDIDATES stories, however many are open."""
    monkeypatch.setattr(pipeline.config, "CLUSTER_CANDIDATES", 2)
    for i in range(4):
        add_article(conn, f"seed{i}", source="NRK", day=day(i + 1))
        cluster_of(conn, f"Sak {i}", day(i + 1), [f"seed{i}"])
    add_article(conn, "new", source="VG", day=day(0))

    seen = answers(monkeypatch, [Assignment(handle="S1", label="Sak 0")])
    assert pipeline.cluster_pending(conn) == (1, 0, 0)
    assert len(seen[0]["candidates"]) == 2


def test_turned_down_coverage_is_never_offered_a_story(conn, monkeypatch):
    """The join pass is gone. A turned-down article was matched on recency —
    the classifier gives it no topics — so it joined whatever was freshest."""
    add_article(conn, "story", source="Aftenposten", day=day(0))
    cluster_of(conn, "Overgrepssiktet lærer", day(0), ["story"])
    add_article(conn, "wire", source="NRK", day=day(1), relevant=0)

    monkeypatch.setattr(
        pipeline.cluster, "assign", lambda *a, **k: pytest.fail("turned down — never asked")
    )
    assert pipeline.cluster_pending(conn) == (0, 0, 0)
    assert labels_of(conn)["wire"] is None


def test_cluster_pending_shortlists_by_topic_not_recency(conn, monkeypatch):
    """The story that went quiet still reaches the prompt if the topics match."""
    monkeypatch.setattr(pipeline.config, "CLUSTER_CANDIDATES", 1)
    add_article(conn, "old", source="NRK", day=day(20), topics=["mobilforbud"])
    cluster_of(conn, "Mobilforbud", day(20), ["old"])
    add_article(conn, "fresh", source="VG", day=day(1), topics=["eksamen"])
    cluster_of(conn, "Eksamen", day(1), ["fresh"])
    add_article(conn, "new", source="Khrono", day=day(0), topics=["mobilforbud"])

    seen = answers(monkeypatch, [Assignment(handle="S1", label="Mobilforbud")])
    assert pipeline.cluster_pending(conn) == (1, 0, 0)
    assert seen[0]["candidates"] == ["Mobilforbud"]


# --- covered, and still worth reading -----------------------------------------


def test_a_follow_up_round_trips_to_the_story_page(conn):
    add_article(conn, "a", source="Khrono", day=day(0))
    storage.record_coverage(
        conn, "a",
        status="oppfolging", method="modell",
        ours={"id": 7, "url": "https://utdanningsnytt.no/7",
              "title": "Høyre vil granske lærerutdanningen", "day": day(3)},
        rationale="Samme forslag om ekspertpanel.",
        whats_new="Lærerutdannerne avviser forslaget.",
        asked=True,
    )
    conn.commit()

    got = queries.story_coverage(queries.get_article(conn, "a"))
    assert got["level"] == "oppfolging"
    assert got["whats_new"] == "Lærerutdannerne avviser forslaget."
    assert got["title"] == "Høyre vil granske lærerutdanningen"


@pytest.mark.parametrize(
    "status,shows_badge",
    (("sitert", True), ("dekket", True), ("oppfolging", True), ("ingen", False)),
)
def test_which_verdicts_reach_the_story_page(conn, status, shows_badge):
    add_article(conn, "a", source="Khrono", day=day(0))
    storage.record_coverage(
        conn, "a", status=status,
        ours={"id": 7, "url": "u", "title": "Vår sak", "day": day(1)},
    )
    conn.commit()
    assert (queries.story_coverage(queries.get_article(conn, "a")) is not None) is shows_badge


def test_existing_database_gains_the_whats_new_column(tmp_path):
    """The deployed coverage table predates the follow-up half."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE coverage (
            article_id TEXT PRIMARY KEY,
            status TEXT NOT NULL, method TEXT,
            internal_id INTEGER, internal_url TEXT, internal_title TEXT,
            internal_day TEXT, rationale TEXT, model TEXT, prompt_version TEXT,
            checks INTEGER NOT NULL DEFAULT 0, checked_at TEXT, error TEXT
        );
        INSERT INTO coverage (article_id, status) VALUES ('a', 'dekket');
        """
    )
    old.commit()
    old.close()

    migrated = storage.connect(path)
    try:
        columns = {r["name"] for r in migrated.execute("PRAGMA table_info(coverage)")}
        assert "whats_new" in columns
        assert migrated.execute("SELECT COUNT(*) FROM coverage").fetchone()[0] == 1
    finally:
        migrated.close()


@pytest.mark.parametrize(
    "match,expected_status",
    (
        (("U1", None), "dekket"),          # covered, nothing added
        (("U1", "Nye tall."), "oppfolging"),  # covered, and it moved on
        (("INGEN", None), "ingen"),
    ),
)
def test_the_pipeline_records_the_verdict_the_model_gave(conn, monkeypatch, match, expected_status):
    from radar.coverage import Match

    handle, whats_new = match
    add_article(conn, "a", source="Khrono", day=day(0))
    conn.execute("UPDATE articles SET body_excerpt = 'tekst' WHERE id = 'a'")
    conn.commit()

    monkeypatch.setattr(
        pipeline.storage, "pending_coverage_check",
        lambda *a, **k: [dict(queries.get_article(conn, "a"))],
    )
    monkeypatch.setattr(
        pipeline.coverage, "check",
        lambda article, candidates: Match(handle=handle, rationale="r", whats_new=whats_new),
    )

    class FakeArchive:
        available = True
        def recent(self, days):
            return [{"id": 7, "url": "u", "title": "Vår sak", "day": day(2), "lead": ""}]

    monkeypatch.setattr(pipeline, "_coverage_by_link", lambda *a, **k: 0)
    pipeline._coverage_by_model(conn, FakeArchive(), limit=10)

    got = conn.execute("SELECT status, whats_new FROM coverage WHERE article_id = 'a'").fetchone()
    assert got["status"] == expected_status


def _coverage_run(conn, monkeypatch, *, match, ours, verified=None):
    """Drive one model-coverage pass over article «a». Returns what it recorded."""
    add_article(conn, "a", source="Khrono", day=day(0))
    conn.execute("UPDATE articles SET body_excerpt = 'tekst' WHERE id = 'a'")
    conn.commit()

    asked: list = []
    monkeypatch.setattr(
        pipeline.storage, "pending_coverage_check",
        lambda *a, **k: [dict(queries.get_article(conn, "a"))],
    )
    monkeypatch.setattr(pipeline.coverage, "check", lambda article, candidates: match)

    def fake_verify(m, pool, *, exclude=None):
        asked.append(exclude)
        return verified if verified is not None else m

    monkeypatch.setattr(pipeline.coverage, "verify_novelty", fake_verify)

    class FakeArchive:
        available = True
        def recent(self, days):
            return ours

    monkeypatch.setattr(pipeline, "_coverage_by_link", lambda *a, **k: 0)
    pipeline._coverage_by_model(conn, FakeArchive(), limit=10)

    row = conn.execute(
        "SELECT status, whats_new, internal_id FROM coverage WHERE article_id = 'a'"
    ).fetchone()
    return dict(row), asked


OURS = [
    {"id": 7, "url": "u7", "title": "Vår sak", "day": "2026-08-04", "lead": "", "body": ""},
    {"id": 8, "url": "u8", "title": "Annen sak", "day": "2026-08-03", "lead": "", "body": ""},
]


def test_a_follow_up_claim_is_put_back_to_the_archive(conn, monkeypatch):
    """The Ystebø case: covered, and the «new» half was already in another piece."""
    from radar.coverage import Match

    match = Match(handle="U1", rationale="Samme utvalg.", whats_new="Ystebø raser.")
    got, asked = _coverage_run(
        conn, monkeypatch, match=match, ours=OURS,
        verified=Match(handle="U1", rationale="Samme utvalg.", whats_new=None),
    )
    assert got == {"status": "dekket", "whats_new": None, "internal_id": 7}
    # Asked about everything except the article the first pass already read.
    assert [o["id"] for o in asked] == [7]


@pytest.mark.parametrize(
    "match_kwargs",
    (
        {"handle": "U1", "rationale": "r"},  # covered, nothing added
        {"handle": "INGEN"},
        {"error": "429 quota"},
    ),
    ids=["covered", "ingen", "error"],
)
def test_nothing_claimed_is_never_put_back(conn, monkeypatch, match_kwargs):
    """The second pass is paid for, so it only runs where a badge is at stake."""
    from radar.coverage import Match

    _, asked = _coverage_run(conn, monkeypatch, match=Match(**match_kwargs), ours=OURS)
    assert asked == []


def test_a_surviving_claim_still_reaches_the_desk(conn, monkeypatch):
    from radar.coverage import Match

    match = Match(handle="U2", rationale="Samme vedtak.", whats_new="Nye tall fra Udir.")
    got, asked = _coverage_run(conn, monkeypatch, match=match, ours=OURS)
    assert got == {
        "status": "oppfolging", "whats_new": "Nye tall fra Udir.", "internal_id": 8
    }
    assert [o["id"] for o in asked] == [8]

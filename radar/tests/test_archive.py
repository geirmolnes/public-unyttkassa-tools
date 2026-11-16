"""Reading our own back catalogue: what comes out, and how much of it."""

import sqlite3

import pytest

from radar.archive import InternalArchive

SCHEMA = """
CREATE TABLE articles (
    article_id INTEGER PRIMARY KEY,
    url TEXT, title TEXT, lead TEXT, body_text TEXT, author TEXT,
    date_published TEXT, keywords TEXT, article_section TEXT,
    source_page TEXT, scraped_at TEXT, date_modified TEXT
);
CREATE TABLE external_links (url TEXT, article_id INTEGER);
"""

# Far enough in the past that a real `days` window still reaches it, near enough
# that no reasonable window excludes it.
DAY = "2026-08-11T08:00:00.000Z"


@pytest.fixture
def archive(tmp_path):
    """A stand-in unytt_artikler.db, one row per test's making."""
    path = tmp_path / "unytt_artikler.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()

    def add(article_id=1, *, title="Vår sak", lead="", body="", section="skole"):
        conn.execute(
            "INSERT INTO articles (article_id, url, title, lead, body_text, "
            "date_published, keywords, article_section) VALUES (?, ?, ?, ?, ?, ?, '', ?)",
            (article_id, f"https://utdanningsnytt.no/{article_id}", title, lead, body,
             DAY, section),
        )
        conn.commit()

    yield add, InternalArchive(path)
    conn.close()


@pytest.mark.parametrize("length,expected", ((5999, 5999), (6000, 6000), (6001, 6000)))
def test_the_body_is_capped(archive, length, expected):
    """Enough text that a quote halfway down a roundup still reaches the check."""
    add, reader = archive
    add(body="Z" * length)
    assert len(reader.recent(days=3650)[0]["body"]) == expected


@pytest.mark.parametrize("length,expected", ((199, 199), (200, 200), (201, 200)))
def test_the_lead_is_capped(archive, length, expected):
    add, reader = archive
    add(lead="Z" * length)
    assert len(reader.recent(days=3650)[0]["lead"]) == expected


def test_a_missing_body_reads_as_empty_not_none(archive):
    """The ranking tokenises this field, so it must never come back None."""
    add, reader = archive
    add(body=None)
    assert reader.recent(days=3650)[0]["body"] == ""


@pytest.mark.parametrize(
    "section,kept",
    (("skole", True), ("barnehage", True), ("debatt", False),
     ("fagartikler", False), ("annonse", False)),
)
def test_what_counts_as_our_reporting(archive, section, kept):
    """A reader's op-ed cites what it argues against; it is not our coverage of it."""
    add, reader = archive
    add(section=section)
    assert (len(reader.recent(days=3650)) == 1) is kept


def test_a_missing_archive_is_not_an_error(tmp_path):
    """A deploy that predates the mount must still tick."""
    reader = InternalArchive(tmp_path / "nope.db")
    assert reader.available is False
    assert reader.recent(days=21) == []
    assert reader.link_index(days=21) == {}

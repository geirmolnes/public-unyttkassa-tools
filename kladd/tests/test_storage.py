"""Tests for kladd.storage."""

import sqlite3

import pytest

import kladd.storage as storage_mod
from kladd.storage import (
    get_cached_source,
    get_draft,
    get_draft_sources,
    get_stats,
    list_drafts,
    save_draft,
    save_profile,
)

from .conftest import make_source


class TestStorage:
    def test_save_and_get_draft(self, tmp_db):
        src = make_source()
        draft_id = save_draft("Article text", "gemini-2.0", [src], user_email="test@x.com")
        draft = get_draft(draft_id)
        assert draft is not None
        assert draft["article"] == "Article text"
        assert draft["model"] == "gemini-2.0"
        assert draft["user_email"] == "test@x.com"

    def test_get_draft_not_found(self, tmp_db):
        assert get_draft(9999) is None

    def test_get_draft_scoped_to_user(self, tmp_db):
        src = make_source()
        draft_id = save_draft("Art", "model", [src], user_email="a@x.com")
        assert get_draft(draft_id, user_email="a@x.com") is not None
        assert get_draft(draft_id, user_email="b@x.com") is None
        # No user_email filter returns the draft (API/service use)
        assert get_draft(draft_id) is not None

    def test_get_draft_sources(self, tmp_db):
        s1 = make_source(source_id="s1")
        s2 = make_source(source_id="s2", source="https://other.com")
        draft_id = save_draft("Art", "model", [s1, s2], user_email="test@x.com")
        sources = get_draft_sources(draft_id)
        assert len(sources) == 2
        assert sources[0]["source_position"] == 0
        assert sources[1]["source_position"] == 1

    def test_list_drafts_ordering(self, tmp_db):
        src = make_source()
        for i in range(3):
            save_draft(f"Art {i}", "model", [src], user_email="test@x.com")
        drafts = list_drafts(user_email="test@x.com")
        assert len(drafts) == 3
        assert drafts[0]["id"] > drafts[1]["id"] > drafts[2]["id"]

    def test_list_drafts_limit(self, tmp_db):
        src = make_source()
        for i in range(3):
            save_draft(f"Art {i}", "model", [src], user_email="test@x.com")
        drafts = list_drafts(limit=2, user_email="test@x.com")
        assert len(drafts) == 2

    def test_list_drafts_user_isolation(self, tmp_db):
        """Each user only sees their own drafts."""
        src = make_source()
        save_draft("A1", "model", [src], user_email="a@x.com")
        save_draft("B1", "model", [src], user_email="b@x.com")
        assert len(list_drafts(user_email="a@x.com")) == 1
        assert len(list_drafts(user_email="b@x.com")) == 1

    def test_pdf_saved_to_disk(self, tmp_db):
        """PDF bytes should be written to disk, path stored in db."""
        src = make_source(pdf_bytes=b"%PDF-1.4 fake content")
        draft_id = save_draft("Art", "model", [src], user_email="test@x.com")
        sources = get_draft_sources(draft_id)
        assert len(sources) == 1
        pdf_path = sources[0]["pdf_path"]
        assert pdf_path is not None
        assert pdf_path.endswith(".pdf")
        from pathlib import Path
        assert Path(pdf_path).read_bytes() == b"%PDF-1.4 fake content"

    def test_no_pdf_path_when_no_bytes(self, tmp_db):
        """Sources without pdf_bytes should have null pdf_path."""
        src = make_source(pdf_bytes=None)
        draft_id = save_draft("Art", "model", [src], user_email="test@x.com")
        sources = get_draft_sources(draft_id)
        assert sources[0]["pdf_path"] is None


class TestCachedSourceTTL:
    URL = "https://example.com/article"

    def _age_drafts(self, hours: int) -> None:
        conn = sqlite3.connect(storage_mod.DB_PATH)
        conn.execute(
            "UPDATE drafts SET created_at = datetime('now', ?)", (f"-{hours} hours",)
        )
        conn.commit()
        conn.close()

    def test_fresh_hit(self, tmp_db):
        save_draft("Art", "model", [make_source()], user_email="x")
        cached = get_cached_source(self.URL)
        assert cached is not None
        assert cached.markdown == "Article body text."

    @pytest.mark.parametrize(
        "hours,expected_hit",
        [(23, True), (24, False), (25, False)],
        ids=["inside-ttl", "at-ttl-exclusive", "past-ttl"],
    )
    def test_ttl_boundary(self, tmp_db, hours, expected_hit):
        save_draft("Art", "model", [make_source()], user_email="x")
        self._age_drafts(hours)
        assert (get_cached_source(self.URL) is not None) is expected_hit

    def test_miss_for_unknown_url(self, tmp_db):
        assert get_cached_source("https://never-seen.example.com") is None


class TestLegacyMigration:
    def test_connect_migrates_pre_draft_id_database(self, tmp_db):
        """Databases created before profiles.draft_id must open cleanly."""
        legacy_schema = """
        CREATE TABLE drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article TEXT NOT NULL, model TEXT NOT NULL, user_email TEXT,
            quotes_ok INTEGER, quotes_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE draft_sources (
            draft_id INTEGER NOT NULL REFERENCES drafts(id),
            source_position INTEGER NOT NULL, source_type TEXT NOT NULL,
            source TEXT NOT NULL, headline TEXT, byline TEXT, pub_date TEXT,
            publication TEXT, markdown TEXT, pdf_path TEXT,
            PRIMARY KEY (draft_id, source_position)
        );
        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline TEXT NOT NULL, model TEXT, source_count INTEGER,
            prompt_chars INTEGER, setup_ms INTEGER, parse_sources_ms INTEGER,
            first_token_ms INTEGER, stream_ms INTEGER, save_draft_ms INTEGER,
            total_ms INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
        conn = sqlite3.connect(storage_mod.DB_PATH)
        conn.executescript(legacy_schema)
        conn.close()

        # Any storage call must trigger the migration without erroring.
        assert get_cached_source("https://example.com/article") is None
        draft_id = save_draft("Art", "model", [make_source()], user_email="x")
        save_profile("draft-stream", {"_total": 5}, draft_id=draft_id)
        assert get_stats()["runs"][0]["total_ms"] == 5


class TestStatsJoin:
    def test_profile_linked_by_draft_id(self, tmp_db):
        draft_id = save_draft("Art", "model", [make_source()], user_email="x")
        save_profile("draft-stream", {"_total": 1234}, draft_id=draft_id)
        runs = get_stats()["runs"]
        assert len(runs) == 1
        assert runs[0]["total_ms"] == 1234

    def test_two_drafts_same_second_do_not_cross_join(self, tmp_db):
        """Each draft gets its own profile; sources must not be duplicated."""
        id1 = save_draft("A", "model", [make_source()], user_email="x")
        id2 = save_draft("B", "model", [make_source()], user_email="x")
        save_profile("draft-stream", {"_total": 100}, draft_id=id1)
        save_profile("draft-stream", {"_total": 200}, draft_id=id2)
        runs = {r["id"]: r for r in get_stats()["runs"]}
        assert runs[id1]["total_ms"] == 100
        assert runs[id2]["total_ms"] == 200
        # One source each — GROUP_CONCAT must not repeat it.
        assert runs[id1]["sources"] == "https://example.com/article"

    def test_legacy_profile_without_draft_id_still_joins(self, tmp_db):
        save_draft("Art", "model", [make_source()], user_email="x")
        save_profile("draft-stream", {"_total": 999})
        # Pin identical timestamps — the legacy fallback joins on created_at.
        conn = sqlite3.connect(storage_mod.DB_PATH)
        conn.execute("UPDATE drafts SET created_at = '2025-01-01 00:00:00'")
        conn.execute("UPDATE profiles SET created_at = '2025-01-01 00:00:00'")
        conn.commit()
        conn.close()
        runs = get_stats()["runs"]
        assert runs[0]["total_ms"] == 999

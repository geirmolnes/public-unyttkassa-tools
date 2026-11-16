"""Tests for kladd.sources."""

from unittest.mock import patch

import pytest
from unytt_parser import SourceType

from kladd.sources import _format_source, load_and_format, parse_urls_eager

from .conftest import make_source


class TestFormatSource:
    def test_full_metadata(self):
        src = make_source()
        result = _format_source(1, src)
        assert "### Kilde 1" in result
        assert "Tittel: Test Headline" in result
        assert "Publikasjon: Test Publication" in result
        assert "Dato: 2025-01-15" in result
        assert "Forfatter: Test Author" in result
        assert "Type: URL" in result
        assert "URL: https://example.com/article" in result
        assert "Article body text." in result

    def test_minimal(self):
        src = make_source(
            headline=None,
            byline=None,
            publication_date=None,
            publication=None,
        )
        result = _format_source(1, src)
        assert "Tittel:" not in result
        assert "Forfatter:" not in result
        assert "Dato:" not in result
        assert "Publikasjon:" not in result
        assert "Type: URL" in result

    def test_url_includes_url(self):
        src = make_source(source_type=SourceType.URL, source="https://example.com")
        result = _format_source(1, src)
        assert "URL: https://example.com" in result

    def test_non_url_omits_url(self):
        src = make_source(source_type=SourceType.PDF, source="doc.pdf")
        result = _format_source(1, src)
        assert "URL:" not in result

    def test_empty_markdown(self):
        src = make_source(markdown=None)
        result = _format_source(1, src)
        assert "(tomt innhold)" in result


class TestLoadAndFormat:
    def test_single_source(self):
        src = make_source()
        with patch("kladd.sources.parse_source", side_effect=[src]):
            loaded = load_and_format(["https://example.com"])
        assert loaded.count == 1
        assert loaded.all_urls is True
        assert len(loaded.parsed) == 1
        assert loaded.dropped == []
        assert "### Kilde 1" in loaded.formatted

    def test_failed_source_reported_and_survivor_renumbered(self):
        bad = make_source(source_id="bad", source="https://bad.com", error="timeout")
        good = make_source(source_id="ok")
        with patch("kladd.sources.parse_source", side_effect=[bad, good]):
            loaded = load_and_format(["a", "b"])
        assert loaded.count == 1
        assert loaded.dropped == [("https://bad.com", "timeout")]
        # Survivor was input #2 but must be labeled Kilde 1 to match the count.
        assert "### Kilde 1" in loaded.formatted
        assert "### Kilde 2" not in loaded.formatted

    def test_all_urls_flag(self):
        s1 = make_source(source_id="u1")
        s2 = make_source(source_id="u2")
        with patch("kladd.sources.parse_source", side_effect=[s1, s2]):
            loaded = load_and_format(["a", "b"])
        assert loaded.all_urls is True

    def test_mixed_types(self):
        url_src = make_source(source_id="u1", source_type=SourceType.URL)
        pdf_src = make_source(source_id="p1", source_type=SourceType.PDF, source="f.pdf")
        with patch("kladd.sources.parse_source", side_effect=[url_src, pdf_src]):
            loaded = load_and_format(["a", "b"])
        assert loaded.all_urls is False

    def test_no_valid_sources(self):
        bad = make_source(error="fail")
        with patch("kladd.sources.parse_source", side_effect=[bad]):
            with pytest.raises(ValueError, match="Ingen gyldige kilder"):
                load_and_format(["a"])


_BLOCKED_REASON = "error: siden blokkerer automatisk henting – last opp som PDF"


@pytest.mark.parametrize(
    "parsed, expected",
    [
        # Regression: oecd.org (Cloudflare challenge) showed a bare ✗ with no reason.
        (
            make_source(error="Failed to fetch URL (blocked, HTTP 403): https://x.test/a"),
            _BLOCKED_REASON,
        ),
        (make_source(error="No article content extracted from URL: https://x.test/a"), "error: fant ingen artikkeltekst"),
        (make_source(error="Failed to fetch URL: https://x.test/a"), "error: kunne ikke leses"),
        (RuntimeError("boom"), "error: kunne ikke leses"),
        (make_source(), "ok"),
    ],
    ids=["blocked-403", "no-content", "generic-fetch-failure", "parser-raises", "ok"],
)
def test_parse_urls_eager_reports_reason(parsed, expected, request):
    url = f"https://x.test/{request.node.callspec.id}"  # unique: ok results are cached
    with (
        patch("kladd.sources.get_cached_source", return_value=None),
        patch("kladd.sources.parse_source", side_effect=[parsed]),
    ):
        assert parse_urls_eager([url]) == {url: expected}

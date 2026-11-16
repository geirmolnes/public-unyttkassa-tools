"""Presentation helpers and the query filters behind the feed."""

import pytest

from radar import config
from radar.source import oslo_day
from radar.web.app import (
    behind_wall,
    coverage_mark,
    domain,
    fmt_date_long,
    fmt_datestamp,
    fmt_time,
    qs_value,
    source_label,
    truncate_chars,
)
from radar.web.queries import COVERED, _predicates
from radar.web.searchq import parse as parse_q

# --- timestamps ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    (
        ("2026-08-06T07:12:33+00:00", "09:12"),  # summer: UTC+2
        ("2026-01-06T07:12:33+00:00", "08:12"),  # winter: UTC+1
        ("2026-08-06T09:12:33+02:00", "09:12"),  # already local, left alone
        ("2026-08-06T07:12", "07:12"),  # no offset: shown as written
        ("2026-08-06T07:1", ""),  # one short
        ("2026-08-06", ""),
        (None, ""),
        ("", ""),
    ),
)
def test_fmt_time(value, expected):
    assert fmt_time(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    (
        ("2026-08-05T22:00:00+00:00", "06.08.26"),  # already tomorrow in Oslo
        ("2026-08-05T21:59:59+00:00", "05.08.26"),
    ),
)
def test_fmt_datestamp_rolls_with_the_local_day(value, expected):
    assert fmt_datestamp(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    (
        ("2026-08-06T07:12:33+00:00", "06.08.26"),
        ("2026-08-06", "06.08.26"),  # exactly long enough
        ("2026-08-0", ""),  # one short
        (None, ""),
    ),
)
def test_fmt_datestamp(value, expected):
    assert fmt_datestamp(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    (
        ("2026-08-06", "6. august 2026"),
        ("2026-01-01", "1. januar 2026"),
        ("2026-12-31", "31. desember 2026"),
        ("2026-13-01", "2026-13-01"),  # month out of range falls back to the raw date
        ("2026-00-01", "2026-00-01"),
        ("2026-08-0", ""),
        (None, ""),
    ),
)
def test_fmt_date_long(value, expected):
    assert fmt_date_long(value) == expected


# --- text ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,limit,expected",
    (
        ("abcde", 5, "abcde"),  # exactly at the limit, untouched
        ("abcdef", 5, "abcde…"),  # one over, and no space to break on
        ("ab cdef", 5, "ab…"),  # breaks on the last space inside the window
        ("abcd", 5, "abcd"),  # one under
        ("  padded  ", 20, "padded"),
        (None, 5, ""),
        ("", 5, ""),
    ),
)
def test_truncate_chars(value, limit, expected):
    assert truncate_chars(value, limit) == expected


@pytest.mark.parametrize(
    "url,expected",
    (
        ("https://www.vg.no/i/abc", "vg.no"),
        ("https://vg.no/i/abc", "vg.no"),
        ("http://www.nrk.no", "nrk.no"),
        ("https://www.tv2.no", "tv2.no"),
        ("vg.no/i/abc", "vg.no"),  # no scheme
        (None, ""),
        ("", ""),
    ),
)
def test_domain(url, expected):
    assert domain(url) == expected


@pytest.mark.parametrize(
    "stored,shown",
    (
        ("Forskning", "Forskning.no"),
        ("Barnehage", "Barnehage.no"),
        ("Khrono", "Khrono"),  # unmapped sources pass through
        (None, ""),
    ),
)
def test_source_label(stored, shown):
    assert source_label(stored) == shown


@pytest.mark.parametrize(
    "value,expected",
    (
        ("NRK:1.17872890", "NRK%3A1.17872890"),
        ("VG:https://vg.no/i/a?b=c", "VG%3Ahttps%3A%2F%2Fvg.no%2Fi%2Fa%3Fb%3Dc"),
        ("a b", "a%20b"),
        ("#frag", "%23frag"),
        (None, ""),
    ),
)
def test_qs_value_escapes_everything(value, expected):
    """Article ids are whole URLs, so nothing may survive as a query separator."""
    assert qs_value(value) == expected


# --- day bucketing ------------------------------------------------------------


@pytest.mark.parametrize(
    "published,expected",
    (
        # August: Oslo is UTC+2, so the day rolls at 22:00 UTC
        ("2026-08-05T21:59:59+00:00", "2026-08-05"),
        ("2026-08-05T22:00:00+00:00", "2026-08-06"),
        # January: UTC+1, so it rolls an hour later
        ("2026-01-05T22:59:59+00:00", "2026-01-05"),
        ("2026-01-05T23:00:00+00:00", "2026-01-06"),
    ),
)
def test_oslo_day_rolls_on_local_midnight(published, expected):
    """A story filed late in the UTC evening belongs to the next Norwegian day."""
    assert oslo_day(published, "") == expected


@pytest.mark.parametrize(
    "published,fallback,expected",
    (
        (None, "2026-08-06T07:00:00+00:00", "2026-08-06"),
        ("ikke en dato", "2026-08-06", "ikke en da"),  # unparseable: first 10 chars
        ("", "2026-03-01T12:00:00+00:00", "2026-03-01"),
    ),
)
def test_oslo_day_falls_back_to_the_cursor(published, fallback, expected):
    assert oslo_day(published, fallback) == expected


# --- feed filters -------------------------------------------------------------


def where_for(q: str):
    return _predicates(parse_q(q))


def test_predicates_default_to_relevant_above_the_threshold():
    sql, params = where_for("")
    assert sql == " AND v.relevant = 1 AND v.score >= ?"
    assert params == [config.FEED_MIN_SCORE]


@pytest.mark.parametrize(
    "query,expected_sql,expected_params",
    (
        (
            "score:0",
            " AND v.relevant = 1 AND v.score >= ?",
            [0],
        ),
        (
            "score:60 trinn:grunnskole",
            " AND v.relevant = 1 AND v.score >= ? AND v.stage IN (?)",
            [60, "grunnskole"],
        ),
        (
            "score:60 type:debatt",
            " AND v.relevant = 1 AND v.score >= ? AND v.kind IN (?)",
            [60, "debatt"],
        ),
        (
            "score:80 trinn:barnehage type:nyhet kilde:NRK",
            " AND v.relevant = 1 AND v.score >= ? AND v.stage IN (?) AND v.kind IN (?)"
            " AND a.source IN (?)",
            [80, "barnehage", "nyhet", "NRK"],
        ),
        (
            # Repeating a key is OR, which is the convention every other search
            # box on the desk's machines already uses.
            "trinn:barnehage trinn:grunnskole",
            " AND v.relevant = 1 AND v.score >= ? AND v.stage IN (?,?)",
            [config.FEED_MIN_SCORE, "barnehage", "grunnskole"],
        ),
    ),
)
def test_predicates_compose_in_parameter_order(query, expected_sql, expected_params):
    """The params list has to line up with the placeholders, in order."""
    sql, params = _predicates(parse_q(query, sources=frozenset({"NRK"})))
    assert sql == expected_sql
    assert params == expected_params


@pytest.mark.parametrize("blank", ("", "   ", None))
def test_blank_query_adds_no_clause(blank):
    sql, params = _predicates(parse_q(blank))
    assert sql == " AND v.relevant = 1 AND v.score >= ?"
    assert params == [config.FEED_MIN_SCORE]


@pytest.mark.parametrize(
    "query,lo,hi",
    (
        ("score:60", 60, None),      # bare number is "this good or better"
        ("score:>60", 61, None),     # strict, and the boundary really is excluded
        ("score:>=60", 60, None),
        ("score:60+", 60, None),
        ("score:<40", None, 39),
        ("score:40-79", 40, 79),
        ("score:79-40", 40, 79),     # written backwards, read the same
        ("score:0", 0, None),
        ("score:100", 100, None),
        ("score:200", 100, None),    # clamped, not rejected
    ),
)
def test_score_boundaries(query, lo, hi):
    q = parse_q(query)
    assert q.min_score == lo
    assert q.max_score == hi


# --- «har vi den?» -------------------------------------------------------------


def row(**overrides) -> dict:
    base = {
        "coverage_status": None,
        "coverage_method": None,
        "coverage_rationale": None,
        "coverage_whats_new": None,
        "internal_title": None,
        "internal_day": None,
        "story_cited": 0,
    }
    return base | overrides


def test_no_mark_when_we_have_nothing():
    assert coverage_mark(row()) is None


def test_a_link_of_ours_is_labelled_as_proof():
    mark = coverage_mark(
        row(coverage_status="sitert", coverage_method="lenke",
            internal_title="Nordtun snur i lydbok-saken", internal_day="2026-08-05")
    )
    assert mark["level"] == "sitert"
    assert mark["label"] == "Sitert av oss"
    assert "«Nordtun snur i lydbok-saken»" in mark["title"]
    assert "5. august 2026" in mark["title"]


def test_a_model_verdict_is_labelled_more_softly_and_explains_itself():
    mark = coverage_mark(
        row(coverage_status="dekket", coverage_method="modell",
            internal_title="Canvas hacket igjen", internal_day="2026-05-05",
            coverage_rationale="Samme datainnbrudd.")
    )
    assert mark["level"] == "dekket"
    assert mark["label"] == "Vi har saken"
    assert mark["title"].endswith("— Samme datainnbrudd.")


def test_a_follow_up_is_marked_as_something_to_read_not_to_skip():
    """Amber, and a label that says do something — the other two say skip."""
    mark = coverage_mark(
        row(coverage_status="oppfolging", coverage_method="modell",
            internal_title="Høyre vil granske lærerutdanningen", internal_day="2026-08-10",
            coverage_whats_new="Lærerutdannerne avviser forslaget.")
    )
    assert mark["level"] == "oppfolging"
    assert mark["label"] == "Ny reaksjon"
    assert mark["title"] == "Lærerutdannerne avviser forslaget."


def test_a_follow_up_without_a_line_still_names_our_article():
    """The tooltip must never be empty — it is the only thing a hover gives."""
    mark = coverage_mark(
        row(coverage_status="oppfolging", coverage_method="modell",
            internal_title="Høyre vil granske lærerutdanningen", internal_day="2026-08-10")
    )
    assert "«Høyre vil granske lærerutdanningen»" in mark["title"]


def test_a_follow_up_counts_as_covered_in_the_filter():
    """It is covered — the badge says read on, the filter says we have it."""
    assert "oppfolging" in COVERED


def test_a_checked_but_uncovered_article_gets_no_mark():
    assert coverage_mark(row(coverage_status="ingen")) is None


def test_proof_on_a_sibling_does_not_cover_this_article():
    """The regression that took the forløp roll-up out.

    Coverage once inherited a cited sibling's proof through the cluster. The two
    steps disagree about doubt by design — clustering joins when unsure, coverage
    refuses when unsure — so the roll-up quietly inverted the strict rule. Every
    badge it ever produced sat on an article the coverage model had already
    answered INGEN for, pulled in by a cluster named after a theme.
    """
    assert coverage_mark(row(coverage_status="ingen", story_cited=1)) is None
    assert coverage_mark(row(story_cited=1)) is None


def test_covered_predicate_reads_only_this_articles_own_row():
    assert "cluster_id" not in COVERED


@pytest.mark.parametrize(
    "query,expected_tail,extra_params",
    (
        ("dekning:ja", f" AND {COVERED}", []),
        ("dekning:nei", f" AND NOT {COVERED}", []),
        ("-dekning:ja", f" AND NOT {COVERED}", []),
        ("", "", []),
        # The three real statuses were always in the column and never reachable
        # from the interface.
        (
            "dekning:sitert",
            " AND EXISTS (SELECT 1 FROM coverage c2 WHERE c2.article_id = a.id AND c2.status = ?)",
            ["sitert"],
        ),
        (
            "dekning:ny",
            " AND EXISTS (SELECT 1 FROM coverage c2 WHERE c2.article_id = a.id AND c2.status = ?)",
            ["oppfolging"],
        ),
    ),
)
def test_coverage_filter_clause(query, expected_tail, extra_params):
    sql, params = _predicates(parse_q(f"score:40 {query}".strip()))
    assert sql == " AND v.relevant = 1 AND v.score >= ?" + expected_tail
    assert params == [40, *extra_params]


# --- the paywall claim ---------------------------------------------------------


@pytest.mark.parametrize(
    "paywalled,body_chars,walled",
    (
        (1, 700, True),      # Computerworld: a teaser, and the flag is right
        (1, 2850, True),     # Aftenposten serves a generous taste, still a taste
        (1, 4000, False),    # Frifagbevegelse: declared gated, whole story served
        (1, None, True),     # nothing to go on — believe the flag
        (0, 500, False),
        (0, 9000, False),
    ),
)
def test_paywall_claim_is_checked_against_the_text_we_have(paywalled, body_chars, walled):
    """The publisher's own flag is a claim; the body is the evidence.

    frifagbevegelse.no marks every article isAccessibleForFree=false and then
    hands over the full text, so «teksten under er så langt kilden slipper oss
    inn» was printed above a complete article.
    """
    assert behind_wall({"paywalled": paywalled, "body_chars": body_chars}) is walled

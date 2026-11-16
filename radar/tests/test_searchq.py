"""The query grammar: what a token means, and what reaches FTS5.

The parser is the only thing allowed to build a MATCH expression, so the tests
that matter most are the ones that hand it hostile input and then make a real
FTS5 table swallow the result.
"""

import sqlite3

import pytest

from radar.web import searchq
from radar.web.searchq import parse

SOURCES = frozenset({"VG", "NRK", "Forskning", "Klassekampen"})
ALIASES = {"Forskning.no": "Forskning", "Barnehage.no": "Barnehage"}


def q(raw, **kw):
    kw.setdefault("sources", SOURCES)
    kw.setdefault("source_aliases", ALIASES)
    return parse(raw, **kw)


# --- keys and values ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    (
        ("trinn:barnehage", ("barnehage",)),
        ("stage:barnehage", ("barnehage",)),          # English alias, never suggested
        ("trinn:vgs", ("videregaende",)),
        ("trinn:videregående", ("videregaende",)),
        ("trinn:lærerutdanning", ("laererutdanning",)),
        ("trinn:tvers", ("tverrgaende",)),
        ("TRINN:Barnehage", ("barnehage",)),          # case-insensitive both halves
        ("trinn:barnehage trinn:grunnskole", ("barnehage", "grunnskole")),
        ("trinn:barnehage trinn:barnehage", ("barnehage",)),  # deduped
        ("trinn:tull", ()),                            # unusable value falls to text
    ),
)
def test_stage_values(raw, expected):
    assert q(raw).stages == expected


@pytest.mark.parametrize(
    "raw,expected",
    (
        ("kilde:VG", ("VG",)),
        ("kilde:vg", ("VG",)),                         # matched case-insensitively
        ("kilde:Forskning.no", ("Forskning",)),        # display label maps back
        ("kilde:Aftenposten", ()),                     # not in the database
    ),
)
def test_source_values(raw, expected):
    assert q(raw).sources == expected


def test_negated_source_goes_to_the_other_list():
    parsed = q("-kilde:VG")
    assert (parsed.sources, parsed.not_sources) == ((), ("VG",))


@pytest.mark.parametrize(
    "raw,lo,hi",
    (
        ("score:60", 60, None),        # bare number means "this good or better"
        ("score:>=60", 60, None),
        ("score:60+", 60, None),
        ("score:>60", 61, None),       # strict: the boundary is genuinely excluded
        ("score:<40", None, 39),
        ("score:<=40", None, 40),
        ("score:40-79", 40, 79),
        ("score:79-40", 40, 79),       # written backwards, read the same
        ("score:0", 0, None),
        ("score:1", 1, None),
        ("score:99", 99, None),
        ("score:100", 100, None),
        ("score:101", 100, None),      # clamped, not rejected
        ("score:>100", 101, None),     # nothing scores this; still not an error
    ),
)
def test_score_boundaries(raw, lo, hi):
    parsed = q(raw)
    assert (parsed.min_score, parsed.max_score) == (lo, hi)


@pytest.mark.parametrize(
    "raw,coverage,not_coverage",
    (
        ("dekning:ja", ("ja",), ()),
        ("dekning:nei", ("nei",), ()),
        ("dekning:sitert", ("sitert",), ()),
        ("dekning:ny", ("oppfolging",), ()),           # the badge's word
        ("dekning:oppfølging", ("oppfolging",), ()),
        ("-dekning:ja", (), ("ja",)),
    ),
)
def test_coverage_values(raw, coverage, not_coverage):
    parsed = q(raw)
    assert (parsed.coverage, parsed.not_coverage) == (coverage, not_coverage)


@pytest.mark.parametrize(
    "raw,paywalled",
    (("mur:ja", True), ("mur:nei", False), ("-mur:ja", False), ("mur:kanskje", None)),
)
def test_paywall_flag(raw, paywalled):
    assert q(raw).paywalled is paywalled


# --- free text and unknown keys -----------------------------------------------


@pytest.mark.parametrize(
    "raw,text",
    (
        ("lærerlønn", "lærerlønn"),
        ("emne:noe", "emne:noe"),
        ("emne:noe fag:x", "emne:noe fag:x"),
        # A headline pasted in must degrade to a search, never to an error.
        ("Ystebø: dette er galskap", "Ystebø: dette er galskap"),
        ("trinn:barnehage lærerlønn", "lærerlønn"),
    ),
)
def test_unknown_keys_become_text(raw, text):
    assert q(raw).text == text


@pytest.mark.parametrize(
    "raw,match",
    (
        # Star policy: last term from 3 characters, others from 4.
        ("lærerlønn", '"lærerlønn"*'),
        ("ab", '"ab"'),                                # 2 chars: never starred
        ("abc", '"abc"*'),                             # 3, and it is the last term
        ("abc def", '"abc" "def"*'),                   # 3 chars, not last: no star
        ("abcd def", '"abcd"* "def"*'),
        ('"barnehagelærer"', '"barnehagelærer"'),      # quoted is an exact phrase
        ("lærer -streik", '"lærer"* NOT "streik"'),
        ("-streik", None),                             # nothing to negate against
        ("", None),
        ("   ", None),
        ("*", None),                                   # the one query that returns all
        ("...", None),
        ("trinn:barnehage", None),                     # structured only: no MATCH
    ),
)
def test_match_expression(raw, match):
    assert q(raw).match == match


@pytest.mark.parametrize(
    "raw",
    (
        'a"b',
        "NEAR(a b)",
        "^caret",
        "(((",
        "a AND b OR c NOT d",
        "col:umn:s",
        "*" * 30,
        "-" * 30,
        'quote" AND "inject',
        "æøå ÆØÅ",
        "x" * 300,
    ),
)
def test_every_expression_is_accepted_by_fts5(raw):
    """Whatever the parser emits, a real FTS5 table has to run it."""
    match = q(raw).match
    if match is None:
        return
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE VIRTUAL TABLE t USING fts5(a, "
        "tokenize='unicode61 remove_diacritics 0', prefix='2 3 4')"
    )
    db.execute("INSERT INTO t(a) VALUES ('lærerlønn i grunnskolen')")
    db.execute("SELECT count(*) FROM t WHERE t MATCH ?", (match,)).fetchone()


# --- caps ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "chars,kept",
    ((199, 199), (200, 200), (201, 200), (5000, 200)),
)
def test_raw_is_capped_at_200_characters(chars, kept):
    assert len(q("a" * chars).raw) == kept


@pytest.mark.parametrize("count,tokens", ((11, 11), (12, 12), (13, 12), (40, 12)))
def test_at_most_twelve_tokens_are_read(count, tokens):
    raw = " ".join(f"trinn:barnehage{i}" for i in range(count))
    # Each is an unusable stage value, so each lands in the text instead — the
    # count of words is what the cap is visible through.
    assert len(q(raw).text.split()) == tokens


# --- mode ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,mode",
    (
        ("", "time"),
        ("trinn:barnehage", "time"),          # filtering is still "the feed"
        ("lærerlønn", "rank"),                # text switches the view
        ("lærerlønn sorter:dato", "time"),    # override wins
        ("sorter:relevans", "rank"),          # even with no text
        ("trinn:barnehage sorter:relevans", "rank"),
    ),
)
def test_mode(raw, mode):
    assert q(raw).mode == mode


# --- chips and rewriting ------------------------------------------------------


def test_chip_removal_drops_exactly_one_token():
    parsed = q("trinn:grunnskole dekning:nei lærerlønn")
    chip = parsed.chips[0]
    assert parsed.without(chip) == "dekning:nei lærerlønn"


@pytest.mark.parametrize(
    "raw,key,value,expected",
    (
        ("lærerlønn", "trinn", "barnehage", "lærerlønn trinn:barnehage"),
        ("trinn:vgs lærerlønn", "trinn", "barnehage", "lærerlønn trinn:barnehage"),
        ("trinn:vgs lærerlønn", "trinn", None, "lærerlønn"),
        ("", "sorter", "dato", "sorter:dato"),
    ),
)
def test_with_token_replaces_in_place(raw, key, value, expected):
    assert q(raw).with_token(key, value) == expected


@pytest.mark.parametrize(
    "kwargs,expected",
    (
        ({"stage": "barnehage"}, "trinn:barnehage"),
        ({"covered": "nei"}, "dekning:nei"),
        ({"kind": "debatt", "source": "VG"}, "type:debatt kilde:VG"),
        ({"source": "Dagens Medisin"}, 'kilde:"Dagens Medisin"'),
        # The default threshold is what an unfiltered feed already means, so it
        # would only add noise to the bar.
        ({"min_score": 0}, ""),
        ({"min_score": 40}, "score:40"),
        ({"min_score": 80}, "score:80"),
        ({}, ""),
    ),
)
def test_from_legacy_params(kwargs, expected):
    assert searchq.from_legacy(**kwargs) == expected


@pytest.mark.parametrize(
    "raw,page,expected",
    (
        ("", None, "/"),
        ("", 1, "/"),
        ("lærer", None, "/?q=l%C3%A6rer"),
        ("lærer", 2, "/?q=l%C3%A6rer&page=2"),
        ("", 3, "/?page=3"),
    ),
)
def test_qs(raw, page, expected):
    assert searchq.qs(raw, page=page) == expected


@pytest.mark.parametrize("page,expected", ((0, 1), (1, 1), (2, 2)))
def test_page_never_goes_below_one(page, expected):
    assert q("", page=page).page == expected

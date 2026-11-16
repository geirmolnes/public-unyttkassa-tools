"""The lexical gate. Whatever it drops is never seen again, so the thresholds matter."""

import pytest

from radar.prefilter import prefilter

# Deliberately free of education vocabulary — pinned by test_filler_alone_is_dropped.
FILLER = "Baten laa ved kaia og vaeret var fint hele helgen. "


def gate(body, *, source="NRK", title=None, summary=None):
    return prefilter(source=source, title=title, summary=summary, body=body)


def test_filler_alone_is_dropped():
    """Guards every other case in this file: the padding must score nothing."""
    result = gate(FILLER * 40)
    assert result.passed is False
    assert result.reason == "no education vocabulary"
    assert result.terms == []


# --- the source short-circuit -------------------------------------------------


@pytest.mark.parametrize("source", ("ViLärare", "Barnehage", "Folkeskolen"))
def test_education_native_source_always_passes(source):
    result = gate(FILLER * 40, source=source)
    assert result.passed is True
    assert result.reason == "education-native source"


def test_unknown_source_gets_no_free_pass():
    assert gate(FILLER * 40, source="Barnehage.no").passed is False


# --- the lede window: 500 characters ------------------------------------------


def test_term_ending_exactly_at_the_lede_edge_counts_as_lede():
    body = "x" * 495 + "skole" + FILLER * 20
    result = gate(body)
    assert result.reason == "strong term in title/lede"
    assert result.head_terms == ["skole"]


def test_term_starting_one_char_past_the_edge_is_body_only():
    """A single root past the lede is not enough — it needs sustained company."""
    body = "x" * 500 + "skole" + FILLER * 20
    result = gate(body)
    assert result.passed is False
    assert result.body_terms == ["skole"]


def test_term_in_title_passes_regardless_of_body():
    result = gate(FILLER * 40, title="Nye nasjonale prover for elevene")
    assert result.reason == "strong term in title/lede"


def test_term_in_summary_passes():
    result = gate(FILLER * 40, summary="Rektor varsler endringer")
    assert result.reason == "strong term in title/lede"


# --- distinct roots in the body: three ----------------------------------------


@pytest.mark.parametrize(
    "roots,passes",
    (
        (["skole"], False),
        (["skole", "barnehage"], False),  # one below
        (["skole", "barnehage", "rektor"], True),  # exactly at
        (["skole", "barnehage", "rektor", "eksamen"], True),  # above
    ),
)
def test_body_needs_three_distinct_roots(roots, passes):
    body = "y" * 500 + " ".join(roots)
    result = gate(body)
    assert result.passed is passes
    if passes:
        assert result.reason == "sustained strong terms in body"


# --- total occurrences in the body: six ---------------------------------------


@pytest.mark.parametrize("repeats,passes", ((4, False), (5, False), (6, True), (7, True)))
def test_body_needs_six_hits_when_the_root_repeats(repeats, passes):
    """One root repeated cannot clear the distinct-root bar, so it must clear the hit bar."""
    body = "y" * 500 + " ".join(["skole"] * repeats)
    result = gate(body)
    assert result.passed is passes
    assert result.body_hits == repeats
    assert result.body_terms == ["skole"]


# --- vocabulary edges ---------------------------------------------------------


@pytest.mark.parametrize(
    "word,is_education",
    (
        ("elev", True),
        ("elever", True),
        ("elevator", False),  # stopword — the stem would otherwise match
        ("elevatoren", False),
        ("lærerike", False),
    ),
)
def test_stopwords_do_not_count_as_stems(word, is_education):
    result = gate(None, title=f"En sak om {word} i dag")
    assert result.passed is is_education


@pytest.mark.parametrize(
    "text,is_education",
    (
        ("Nytt om SFO til hosten", True),
        ("Nytt om PISA til hosten", True),
        ("Nytt om sfo til hosten", False),  # acronyms are matched on the original case
    ),
)
def test_acronyms_are_case_sensitive(text, is_education):
    assert gate(None, title=text).passed is is_education


def test_terms_are_deduplicated_and_sorted():
    result = gate(None, title="Skole og skole og barnehage")
    assert result.terms == ["barnehage", "skole"]


@pytest.mark.parametrize("body", (None, ""))
def test_empty_body_without_a_title_is_dropped(body):
    result = gate(body)
    assert result.passed is False
    assert result.body_hits == 0


# --- Nordic word-forms the roots have to reach --------------------------------


@pytest.mark.parametrize(
    "title,expected",
    (
        ("Helt ny skola invigd i Arboga", ["skola"]),
        ("Kommunen lägger ned tre skolor", ["skolor"]),  # sv plural, not inside «skola»
        ("Skolorna stänger i Norrland", ["skolor"]),
        ("Grundskolor rustas upp", ["skolor"]),  # the plural as head of a compound
        ("Inskolning i förskolan", ["förskol"]),
        ("Nytt gymnasium byggs i Malmö", ["gymnasium"]),  # not inside «gymnasie»
        ("Satsning på barnomsorgen i budgeten", ["barnomsorg"]),
        ("Daginstitutioner mangler pædagoger", ["daginstitution", "pædagog"]),
        ("Folkeskolen mangler vikarer", ["skole"]),
        ("Fritidshemmen är underbemannade", ["fritidshem"]),
    ),
)
def test_swedish_and_danish_forms_reach_a_compound_root(title, expected):
    result = gate(None, title=title)
    assert result.passed is True
    assert result.terms == expected


@pytest.mark.parametrize("title", ("Gymnastikk for de eldste", "Han er en dyktig gymnast"))
def test_gymnastics_is_not_a_school_stage(title):
    """«gymnasium» is spelled out so the shorter «gymnas» cannot swallow these."""
    assert gate(None, title=title).passed is False

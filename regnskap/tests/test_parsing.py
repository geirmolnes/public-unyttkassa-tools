"""Org number parsing and year selection."""

import pytest

from src.parsing import normalize_orgnr, orgnr_error, parse_orgnr_list, select_years


@pytest.mark.parametrize(
    "text,expected",
    [
        ("998812623", "998812623"),
        ("998 812 623", "998812623"),
        (" 998812623 ", "998812623"),
        ("998 812 623", "998812623"),
        ("99881262", None),
        ("9988126230", None),
        ("99881262x", None),
        ("", None),
    ],
    ids=["plain", "spaces", "padded", "nbsp", "8-digits", "10-digits", "letter", "empty"],
)
def test_normalize_orgnr(text, expected):
    assert normalize_orgnr(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("998812623", None),
        ("998 812 623", None),
        ("99881262", "Organisasjonsnummer må ha 9 siffer (fikk 8)"),
        ("9988126230", "Organisasjonsnummer må ha 9 siffer (fikk 10)"),
        ("123456789", "Ugyldig organisasjonsnummer (feil kontrollsiffer)"),
        ("998812624", "Ugyldig organisasjonsnummer (feil kontrollsiffer)"),
        ("HJR HOLDING", "Ikke et organisasjonsnummer"),
        ("", "Ikke et organisasjonsnummer"),
    ],
    ids=[
        "valid",
        "valid-spaces",
        "8-digits",
        "10-digits",
        "bad-checksum",
        "off-by-one-digit",
        "name",
        "empty",
    ],
)
def test_orgnr_error(text, expected):
    assert orgnr_error(text) == expected


@pytest.mark.parametrize(
    "text,orgnrs,invalid",
    [
        ("998812623\n934222695", ["998812623", "934222695"], []),
        ("998812623, 934222695;927110423", ["998812623", "934222695", "927110423"], []),
        ("998 812 623\r\n934 222 695", ["998812623", "934222695"], []),
        ("998812623\n998 812 623", ["998812623"], []),
        ("998812623 HJR HOLDING AS", ["998812623"], []),
        ("998812623 934222695", ["998812623", "934222695"], []),
        ("orgnr\n998812623", ["998812623"], [("orgnr", "Ikke et organisasjonsnummer")]),
        ("12345", [], [("12345", "Organisasjonsnummer må ha 9 siffer (fikk 5)")]),
        ("9988126230", [], [("9988126230", "Organisasjonsnummer må ha 9 siffer (fikk 10)")]),
        ("123456789", [], [("123456789", "Ugyldig organisasjonsnummer (feil kontrollsiffer)")]),
        ("\n\n , ;", [], []),
    ],
    ids=[
        "newlines",
        "commas-semicolons",
        "spaced-crlf",
        "dedupe",
        "trailing-name",
        "space-separated",
        "header-line",
        "too-short",
        "too-long",
        "bad-checksum",
        "only-separators",
    ],
)
def test_parse_orgnr_list(text, orgnrs, invalid):
    parsed = parse_orgnr_list(text)
    assert (parsed.orgnrs, parsed.invalid) == (orgnrs, invalid)


AVAILABLE = [2012, 2013, 2014, 2015, 2016]


@pytest.mark.parametrize(
    "mode,kwargs,expected",
    [
        ("all", {}, [2012, 2013, 2014, 2015, 2016]),
        ("years", {"years": [2016, 2013]}, [2013, 2016]),
        ("years", {"years": [2013, 1999]}, [2013]),
        ("years", {"years": []}, []),
        ("last", {"last_n": 1}, [2016]),
        ("last", {"last_n": 3}, [2014, 2015, 2016]),
        ("last", {"last_n": 4}, [2013, 2014, 2015, 2016]),
        ("last", {"last_n": 5}, [2012, 2013, 2014, 2015, 2016]),
        ("last", {"last_n": 6}, [2012, 2013, 2014, 2015, 2016]),
        ("range", {"from_year": 2013, "to_year": 2015}, [2013, 2014, 2015]),
        ("range", {"from_year": 2014, "to_year": 2014}, [2014]),
        ("range", {"from_year": 2016, "to_year": 2030}, [2016]),
        ("range", {"from_year": 2017, "to_year": 2030}, []),
        ("range", {"from_year": 2000, "to_year": 2011}, []),
        ("range", {"from_year": 2000, "to_year": 2012}, [2012]),
    ],
    ids=[
        "all",
        "years-unsorted",
        "years-missing",
        "years-empty",
        "last-1",
        "last-3",
        "last-len-minus-1",
        "last-len",
        "last-len-plus-1",
        "range",
        "range-single",
        "range-at-top-edge",
        "range-above-top",
        "range-below-bottom",
        "range-at-bottom-edge",
    ],
)
def test_select_years(mode, kwargs, expected):
    assert select_years(AVAILABLE, mode, **kwargs) == expected


def test_select_years_sorts_and_dedupes_available():
    assert select_years([2014, 2012, 2014], "all") == [2012, 2014]


@pytest.mark.parametrize(
    "mode,kwargs,message",
    [
        ("last", {"last_n": 0}, "Antall år må være minst 1"),
        ("last", {"last_n": -1}, "Antall år må være minst 1"),
        ("last", {}, "Antall år må være minst 1"),
        ("range", {"from_year": 2015, "to_year": 2014}, "Fra-år kan ikke være etter til-år"),
        ("range", {"from_year": 2015}, "Både fra- og til-år må fylles ut"),
        ("bogus", {}, "Ukjent utvalg: bogus"),
    ],
    ids=[
        "last-0",
        "last-negative",
        "last-missing",
        "range-reversed",
        "range-missing-to",
        "unknown-mode",
    ],
)
def test_select_years_rejects_bad_input(mode, kwargs, message):
    with pytest.raises(ValueError, match=f"^{message}$"):
        select_years(AVAILABLE, mode, **kwargs)

"""Tests for kladd.quote_check — extraction, literal matching, verdicts, edits."""

import pytest

from kladd.quote_check import (
    apply_llm_verdicts,
    apply_quote_edits,
    check_quotes_detailed,
    extract_quotes,
    nest_guillemets,
    normalize_inner_quotes_in_dash_quotes,
    strip_guillemets_in_dash_quotes,
)
from kladd.quote_verify import QuoteVerdict


def make_verdict(
    quote: str = "q",
    *,
    ok: bool = False,
    source_snippet: str | None = None,
    corrected_quote: str | None = None,
    reason: str = "test",
) -> QuoteVerdict:
    return QuoteVerdict(
        quote=quote,
        ok=ok,
        source_snippet=source_snippet,
        corrected_quote=corrected_quote,
        reason=reason,
    )


@pytest.mark.parametrize(
    "article,expected",
    [
        ("– Dette er et sitat, sier NN.", ["Dette er et sitat, sier NN."]),
        ("— Em-dash-sitat her.", ["Em-dash-sitat her."]),
        ("–", []),
        ("Vanlig tekst uten sitater.", []),
        # Inline «» threshold is 4 words: 3 below, 4 at, 5 above.
        ("Han kalte det «tre ord her» i går.", []),
        ("Han kalte det «fire ord er her» i går.", ["fire ord er her"]),
        ("Han kalte det «fem ord er her nå» i går.", ["fem ord er her nå"]),
        # Nested «» inside a dash line is part of that quote, not separate.
        (
            "– Vi sa «aldri mer atten prosent lønnsvekst», sier NN.",
            ["Vi sa «aldri mer atten prosent lønnsvekst», sier NN."],
        ),
        # Two inline quotes on one line are both extracted.
        (
            "Hun sa «første sitat på fire ord» og «andre sitat på fire ord».",
            ["første sitat på fire ord", "andre sitat på fire ord"],
        ),
    ],
    ids=[
        "dash-line",
        "em-dash-line",
        "empty-dash",
        "no-quotes",
        "inline-3-words",
        "inline-4-words",
        "inline-5-words",
        "nested-in-dash",
        "two-inline",
    ],
)
def test_extract_quotes(article, expected):
    assert extract_quotes(article) == expected


@pytest.mark.parametrize(
    "line,expected_line,expected_edits",
    [
        ("– «Sitatet her», sier NN.", "– Sitatet her, sier NN.", 1),
        ('– "Sitatet her", sier NN.', "– Sitatet her, sier NN.", 1),
        ("– Uten anførselstegn, sier NN.", "– Uten anførselstegn, sier NN.", 0),
        ("Vanlig avsnitt med «sitat» i teksten.", "Vanlig avsnitt med «sitat» i teksten.", 0),
        ("  – «Innrykket», sier NN.", "  – Innrykket, sier NN.", 1),
    ],
    ids=["guillemets", "ascii-quotes", "clean-dash", "non-dash", "indented"],
)
def test_strip_guillemets_in_dash_quotes(line, expected_line, expected_edits):
    result, edits = strip_guillemets_in_dash_quotes(line)
    assert result == expected_line
    assert edits == expected_edits


@pytest.mark.parametrize(
    "line,expected_line,expected_edits",
    [
        ('– Vi kaller det "dugnad", sier NN.', "– Vi kaller det «dugnad», sier NN.", 1),
        ("– Vi kaller det “dugnad”, sier NN.", "– Vi kaller det «dugnad», sier NN.", 1),
        ("– Vi kaller det 'dugnad', sier NN.", "– Vi kaller det «dugnad», sier NN.", 1),
        ("– Don't panic, sier NN.", "– Don't panic, sier NN.", 0),
        ('Utenfor sitat: "dugnad" står urørt.', 'Utenfor sitat: "dugnad" står urørt.', 0),
    ],
    ids=["ascii-double", "curly-double", "single", "contraction", "non-dash"],
)
def test_normalize_inner_quotes(line, expected_line, expected_edits):
    result, edits = normalize_inner_quotes_in_dash_quotes(line)
    assert result == expected_line
    assert edits == expected_edits


@pytest.mark.parametrize(
    "line,expected_line,expected_edits",
    [
        (
            "I rapporten står det: «Klager melder om «feilaktig gjengivelse». Dette er dokumentert.»",
            "I rapporten står det: «Klager melder om ‹feilaktig gjengivelse›. Dette er dokumentert.»",
            2,
        ),
        ("Ett nivå: «helt greit» står urørt.", "Ett nivå: «helt greit» står urørt.", 0),
        ("Allerede riktig: «om ‹dette› her».", "Allerede riktig: «om ‹dette› her».", 0),
    ],
    ids=["nested", "single-level", "already-nested"],
)
def test_nest_guillemets(line, expected_line, expected_edits):
    result, edits = nest_guillemets(line)
    assert result == expected_line
    assert edits == expected_edits


SOURCE = "Kommunen bruker mer penger enn budsjettert på skole i år."


@pytest.mark.parametrize(
    "verb",
    ["sier", "sa", "skriver", "forteller", "fortalte", "opplyser", "mener",
     "understreker", "påpeker", "fastslår", "uttaler", "forklarer",
     "fortsetter", "legger", "ifølge"],
)
def test_attribution_verbs_stripped_before_matching(verb):
    """Quote core exists in source; attribution tail must be stripped for the match."""
    article = f"– {SOURCE[:-1]}, {verb} Kari Nordmann til VG."
    details = check_quotes_detailed(article, [SOURCE])
    assert details[0]["ok"] is True
    assert details[0]["verified_by"] == "literal"


def test_unknown_attribution_verb_fails_literal():
    article = f"– {SOURCE[:-1]}, hvisker Kari Nordmann til VG."
    details = check_quotes_detailed(article, [SOURCE])
    assert details[0]["ok"] is False


@pytest.mark.parametrize(
    "tail_len,expected_ok",
    [(2, True), (80, True), (81, False)],
    ids=["min-tail", "at-cap", "over-cap"],
)
def test_attribution_tail_length_bounds(tail_len, expected_ok):
    """Attribution tail is stripped only when 2–80 chars long."""
    article = f"– {SOURCE[:-1]}, sier {'N' * tail_len}"
    details = check_quotes_detailed(article, [SOURCE])
    assert details[0]["ok"] is expected_ok


class TestCheckQuotesDetailed:
    def test_literal_match(self):
        details = check_quotes_detailed(f"– {SOURCE}", [SOURCE])
        assert details == [
            {
                "quote": SOURCE,
                "ok": True,
                "source": SOURCE,
                "verified_by": "literal",
            }
        ]

    def test_no_quotes_returns_empty(self):
        assert check_quotes_detailed("Ingen sitater her.", [SOURCE]) == []

    def test_missing_quote_fails(self):
        details = check_quotes_detailed("– Dette står ikke i kilden.", [SOURCE])
        assert details[0]["ok"] is False
        assert details[0]["verified_by"] is None

    def test_non_bokmal_marker_forces_failure(self):
        quote = "Vi har arbejdet hårdt i mange år."
        details = check_quotes_detailed(f"– {quote}", [quote])
        assert details[0]["ok"] is False
        assert details[0]["marker_hit"] is True

    @pytest.mark.parametrize(
        "url,expected_thorough",
        [
            ("https://folkeskolen.dk/artikel", True),
            ("https://lararen.se/nyhet", True),
            ("https://khrono.no/sak", False),
            ("https://x.dk.example.com/sak", False),
        ],
        ids=["dk", "se", "no", "dk-subdomain-of-com"],
    )
    def test_thorough_tld(self, url, expected_thorough):
        details = check_quotes_detailed(f"– {SOURCE}", [SOURCE], [url])
        assert details[0]["ok"] is (not expected_thorough)
        assert details[0].get("thorough", False) is expected_thorough

    def test_inline_quote_verified(self):
        article = f"Han sa «{SOURCE[:-1]}» under møtet."
        details = check_quotes_detailed(article, [SOURCE])
        assert details[0]["ok"] is True


class TestVerdictsAndEdits:
    def test_llm_accepts_failing_quote(self):
        details = check_quotes_detailed("– Dette står ikke i kilden.", [SOURCE])
        apply_llm_verdicts(details, [make_verdict(ok=True, source_snippet="snippet")])
        assert details[0]["ok"] is True
        assert details[0]["verified_by"] == "llm"
        assert details[0]["source"] == "snippet"

    def test_corrected_quote_applied(self):
        article = "– Dette står ikke i kilden."
        details = check_quotes_detailed(article, [SOURCE])
        apply_llm_verdicts(
            details, [make_verdict(corrected_quote="Dette står i kilden.")]
        )
        new_article, edits = apply_quote_edits(article, details)
        assert new_article == "– Dette står i kilden."
        assert edits == 1
        assert details[0]["ok"] is True

    def test_corrected_quote_leading_dash_stripped(self):
        article = "– Dette står ikke i kilden."
        details = check_quotes_detailed(article, [SOURCE])
        apply_llm_verdicts(
            details, [make_verdict(corrected_quote="– Dette står i kilden.")]
        )
        new_article, _ = apply_quote_edits(article, details)
        assert new_article == "– Dette står i kilden."
        assert "– –" not in new_article

    def test_marker_hit_llm_ok_downgraded(self):
        quote = "Vi har arbejdet hårdt."
        details = check_quotes_detailed(f"– {quote}", [quote])
        apply_llm_verdicts(details, [make_verdict(ok=True)])
        assert details[0]["ok"] is False
        assert details[0]["verified_by"] == "llm-overridden"

    def test_translation_residue_applied_but_not_ok(self):
        quote = "Vi har arbejdet hårdt i mange år."
        article = f"– {quote}"
        details = check_quotes_detailed(article, [quote])
        # "Correction" still contains a Danish marker (arbejdet).
        apply_llm_verdicts(
            details, [make_verdict(corrected_quote="Vi har arbejdet mye i mange år.")]
        )
        new_article, edits = apply_quote_edits(article, details)
        assert edits == 1
        assert "arbejdet mye" in new_article
        assert details[0]["ok"] is False
        assert details[0]["translation_residue"] is True


class TestNynorskMode:
    NN_QUOTE = "Vi veit ikkje kva som skjer, og det er berre trist."

    def test_nynorsk_quote_flags_in_bokmal_mode(self):
        details = check_quotes_detailed(f"– {self.NN_QUOTE}", [self.NN_QUOTE])
        assert details[0]["ok"] is False
        assert details[0]["marker_hit"] is True

    def test_nynorsk_quote_passes_in_nynorsk_mode(self):
        details = check_quotes_detailed(
            f"– {self.NN_QUOTE}", [self.NN_QUOTE], nynorsk=True
        )
        assert details[0]["ok"] is True
        assert details[0]["verified_by"] == "literal"

    @pytest.mark.parametrize("nynorsk", [False, True], ids=["bokmal", "nynorsk"])
    def test_danish_quote_flags_in_both_modes(self, nynorsk):
        quote = "Vi har arbejdet hårdt i mange år."
        details = check_quotes_detailed(f"– {quote}", [quote], nynorsk=nynorsk)
        assert details[0]["ok"] is False
        assert details[0]["marker_hit"] is True

    @pytest.mark.parametrize("nynorsk", [False, True], ids=["bokmal", "nynorsk"])
    def test_swedish_quote_flags_in_both_modes(self, nynorsk):
        quote = "Det finns mycket kvar att göra, säger hon."
        details = check_quotes_detailed(f"– {quote}", [quote], nynorsk=nynorsk)
        assert details[0]["ok"] is False
        assert details[0]["marker_hit"] is True

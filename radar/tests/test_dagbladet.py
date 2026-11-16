"""Dagbladet: live-blog links dropped, and the hyphens CommonMark reads as bullets."""

import pytest

from radar.dagbladet import is_studio_post
from radar.text import article_html

LEDE = "Dette er en helt vanlig innledning som er lang nok til å telle som brødtekst i artikkelen."
QUOTE = "- Jeg ser stor forskjell, forteller Rosenborg-kjenner Kent Aune til Dagbladet."

# The party donations Dagbladet really does print as bullets, ending open.
DONATIONS = "- Høyre: 21 355 273 kroner\n- Frp: 13 500 000 kroner\n- Venstre: 3 000 000 kroner"


def body(*blocks: str) -> str:
    """An article body with the given blocks under a normal lede."""
    return "\n\n".join([LEDE, *blocks])


def db(*blocks: str) -> str:
    return article_html(body(*blocks), source="Dagbladet")


# --- the conversion itself ----------------------------------------------------


def test_quote_dash_becomes_a_paragraph():
    """The marker is a quote dash, and the page should print it as one."""
    html = db(QUOTE)
    assert "<ul>" not in html
    assert (
        "<p>– Jeg ser stor forskjell, forteller Rosenborg-kjenner Kent Aune til Dagbladet.</p>"
        in html
    )


def test_consecutive_quotes_become_separate_paragraphs():
    html = db("- Det er aldri hyggelig, sier sportssjefen.\n\n- Vi skilles som venner, sier han.")
    assert "<li>" not in html
    assert html.count("<p>– ") == 2


@pytest.mark.parametrize(
    "source,converted",
    (
        ("Dagbladet", True),
        ("dagbladet", True),
        ("  DAGBLADET  ", True),
        ("NRK", False),
        ("BT", False),
        (None, False),
    ),
)
def test_only_dagbladet_is_corrected(source, converted):
    """Every other source writes "– " already; their hyphens are real bullets."""
    html = article_html(body(QUOTE), source=source)
    assert ("<li>" in html) is not converted


# --- the interviewer's questions ----------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    (
        ("- Hvordan har han det?", "<p><em>– Hvordan har han det?</em></p>"),
        ("- Han har det slitsomt, sier Ibsen.", "<p>– Han har det slitsomt, sier Ibsen.</p>"),
    ),
)
def test_questions_are_italicised(line, expected):
    """Dagbladet italicises the whole question line, dash included — a CSS class
    the scraper's HTML-to-markdown step drops."""
    assert expected in db(line)


# --- what separates a quote from a bullet -------------------------------------


@pytest.mark.parametrize(
    "text,is_quote",
    (
        ("Ja da, sier X.", False),  # 14 chars — one below the floor
        ("Ja da, sier XY.", True),  # exactly at it
        ("Ja da, sier XYZ.", True),  # above
    ),
)
def test_quote_needs_fifteen_characters(text, is_quote):
    assert ("<li>" in db(f"- {text}")) is not is_quote


@pytest.mark.parametrize(
    "text,is_quote",
    (
        ("Javisst, sikkert.", False),  # two words, long enough
        ("Ja visst, sikkert.", True),  # three
        ("Ja visst, det er sikkert.", True),  # four
    ),
)
def test_quote_needs_three_words(text, is_quote):
    """Long enough is not enough: "Sjefredaktør og admin direktør:" is a rail."""
    assert ("<li>" in db(f"- {text}")) is not is_quote


@pytest.mark.parametrize(
    "ending,is_quote",
    (
        ("Vi var levende døde, sier hun.", True),
        ("Det var helt utrolig, roper han!", True),
        ("Hva blir den største forskjellen?", True),
        ("Han venter fortsatt på svaret …", True),
        ("«Ingen skilles som uvenner», skrev han»", True),
        ("Aune svarer dette - og fortsetter:", True),  # a quote continued below
        ("Ole Gunnar Solskjær (klubbløs)", False),  # a name in a candidate list
        ("Arsenal - Atlético Madrid 1-0", False),  # a results table
        ("Høyre: 21 355 273 kroner", False),  # a donations table
    ),
)
def test_quote_has_to_close_like_a_sentence(ending, is_quote):
    assert ("<li>" in db(f"- {ending}")) is not is_quote


def test_quote_closing_on_a_link_is_still_a_quote():
    """The raw markdown ends in a bracket; the page ends in a full stop."""
    html = db("- Det er en del av hans gave til oss, fortalte Solberg til [VG.](https://vg.no/x)")
    assert "<li>" not in html
    assert "<p>– Det er en del av" in html


def test_multi_block_item_stays_a_bullet():
    """A nested list is a list, whatever its first line looks like."""
    html = db("- Ytre punkt som er en hel setning.\n  - Indre punkt som er en hel setning.")
    assert html.count("<li>") == 2


# --- lists Dagbladet really does write ----------------------------------------


def test_real_bullet_list_is_left_alone():
    html = db("Pengesummen fordeler seg slik:", DONATIONS)
    assert html.count("<li>") == 3
    assert "– Høyre" not in html


def test_mixed_list_splits_and_keeps_its_order():
    """CommonMark merges the donations table and the quote under it into one ul."""
    html = db(f"{DONATIONS}\n\n- Vi mener at Norge må styres i en ny retning, skrev Hagen.")
    assert html.count("<li>") == 3
    assert html.index("</ul>") < html.index("<p>– Vi mener at Norge")


# --- studio posts -------------------------------------------------------------


@pytest.mark.parametrize(
    "source,link,studio",
    (
        ("Dagbladet", "https://www.dagbladet.no/studio/92397-kong-harald-er-død/12403286-møtte-opp", True),
        ("Dagbladet", "https://www.dagbladet.no/studio/72720-nyhetsstudio/12402503-brann", True),
        ("Dagbladet", "https://www.dagbladet.no/studio", True),
        ("Dagbladet", "https://www.dagbladet.no/studio/", True),
        # The same word anywhere else in the path is a headline, not the product.
        ("Dagbladet", "https://www.dagbladet.no/nyheter/studio-brant-ned/85116750", False),
        ("Dagbladet", "https://www.dagbladet.no/studioet/85116750", False),
        ("Dagbladet", "https://www.dagbladet.no/nyheter/alle-avganger-stanses/85116750", False),
        # Scoped: another outlet's /studio/ is that outlet's article.
        ("VG", "https://www.vg.no/studio/12345", False),
        ("Dagbladet", None, False),
        (None, "https://www.dagbladet.no/studio/1/2", False),
    ),
)
def test_is_studio_post(source, link, studio):
    assert is_studio_post(source, link) is studio

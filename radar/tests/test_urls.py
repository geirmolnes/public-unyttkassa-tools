"""The join key: what counts as the same article, and what proves nothing."""

import pytest

from radar.urls import is_page_anchor, normalize

ARTICLE = "https://www.nrk.no/trondelag/elever-med-dysleksi-1.17970004"
KEY = "nrk.no/trondelag/elever-med-dysleksi-1.17970004"


# --- the same article, written down differently -------------------------------


@pytest.mark.parametrize(
    "url",
    (
        ARTICLE,
        ARTICLE + "/",
        ARTICLE + "#hovedinnhold",
        ARTICLE + "?utm_source=chatgpt.com",
        ARTICLE + "?utm_source=nyhetsbrev&utm_medium=email&fbclid=abc",
        ARTICLE.replace("https://www.", "http://"),
        ARTICLE.replace("nrk.no", "NRK.no"),
    ),
)
def test_same_article_same_key(url):
    assert normalize(url) == KEY


def test_mixed_case_article_id_survives():
    """Schibsted ids are mixed case; both sides are lowercased, so they still meet."""
    a = normalize("https://www.bt.no/lokalt/i/Bxr7VE/en-sak")
    b = normalize("http://bt.no/lokalt/i/bxr7ve/en-sak/")
    assert a == b == "bt.no/lokalt/i/bxr7ve/en-sak"


# --- meaningful parameters are kept -------------------------------------------


def test_live_blog_entry_is_not_collapsed():
    """The regression that made this module necessary.

    BT's live blog is one URL for the whole day; `pinnedEntry` is the only thing
    that says which event. Strip the query and a month of unrelated saker share
    one key — which is how a link to the live blog "proves" we covered a story we
    never touched.
    """
    base = "https://www.bt.no/nyheter/direkte/i/BR649v/nyheter-direkte"
    assert normalize(f"{base}?pinnedEntry=334113") != normalize(f"{base}?pinnedEntry=999999")


def test_parameter_order_does_not_matter():
    a = normalize("https://sok.samordnaopptak.no/liste?year=2026&kode=lu")
    b = normalize("https://sok.samordnaopptak.no/liste?kode=lu&year=2026")
    assert a == b


def test_tracking_dropped_but_content_parameter_kept():
    key = normalize("https://www.stortinget.no/sporsmal/?qnid=125564&utm_source=nb")
    assert key == "stortinget.no/sporsmal?qnid=125564"


# --- Outlook's link rewriter ---------------------------------------------------


def test_safelinks_unwrapped_to_the_real_url():
    wrapped = (
        "https://eur02.safelinks.protection.outlook.com/?url="
        "https%3A%2F%2Fhdl.handle.net%2F11250%2F3119936"
        "&data=05%7C02%7Cx%40oslomet.no&reserved=0"
    )
    assert normalize(wrapped) == "hdl.handle.net/11250/3119936"


def test_safelinks_without_a_payload_proves_nothing():
    assert normalize("https://eur02.safelinks.protection.outlook.com/?data=05") is None


# --- links that prove nothing --------------------------------------------------


@pytest.mark.parametrize(
    "url",
    (
        None,
        "",
        "   ",
        "https://www.nrk.no",  # front page
        "https://www.nrk.no/",
        "http://khrono.no/?utm_source=nb",  # front page with a campaign tag
        "mailto:redaksjonen@utdanningsnytt.no",
        "/relativ/lenke",
        "javascript:void(0)",
    ),
)
def test_no_key_for_urls_that_identify_no_article(url):
    assert normalize(url) is None


# --- an item on a page, or the page itself ------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    (
        # The live case: the path names the nyhetssenter page, the fragment names
        # the kortnytt. Sixteen of these shared one page and one another's bodies.
        (
            "https://www.nrk.no/nordland/nyhetssenter-nordland-1.15898380"
            "#1.17992892.1.17992892",
            True,
        ),
        # Same shape on an ordinary article page — the page is a real article,
        # but not *this* one, which is exactly what makes it unservable.
        (
            "https://www.nrk.no/vestland/xl/onsker-seg-nytt-andlet-for-skulestart"
            "-1.17957655#1.17983106.1.17983106",
            True,
        ),
        # The fragment names the id the path already carries: an anchor into the
        # article's own page, which serves the right body.
        ("https://www.nrk.no/trondelag/elever-1.17970004#1.17970004", False),
        # No fragment at all.
        ("https://www.nrk.no/trondelag/elever-1.17970004", False),
        ("https://www.nrk.no/trondelag/elever-1.17970004#", False),
        # Fragments that carry no id are ordinary anchors and must keep working.
        ("https://www.nrk.no/trondelag/elever-1.17970004#hovedinnhold", False),
        ("https://www.nrk.no/trondelag/elever-1.17970004#comments", False),
        # The four-character floor: three digits is an ordinal, four is an id.
        ("https://www.nrk.no/a-1.17970004#12", False),
        ("https://www.nrk.no/a-1.17970004#123", False),
        ("https://www.nrk.no/a-1.17970004#1234", True),
        ("https://www.nrk.no/a-1.17970004#12345", True),
        # Counted after the non-digits are dropped, so a short ordinal stays one.
        ("https://www.nrk.no/a-1.17970004#page-2", False),
        ("https://www.nrk.no/a-1.17970004#section-12", False),
        # One unknown id among several is enough — that one cannot be served.
        ("https://www.nrk.no/a-1.17970004#1.17970004-9876", True),
        # A pinned entry in a Schibsted live blog: the page served is the blog.
        ("https://www.bt.no/nyheter/direkte/i/BR649v/nyheter-direkte?pinnedEntry=337496", True),
        ("https://bt.no/i/8p7y5w?pinnedEntry=338395", True),
        ("https://www.aftenposten.no/norge/i/wAwywd/siste-nytt?utm_source=x&pinnedEntry=124190", True),
        ("https://www.bt.no/i/8p7y5w?pinnedEntry=", True),
        # The live blog itself, and a parameter that only looks like the one.
        ("https://www.bt.no/nyheter/direkte/i/JOp8ym/politiet-mann-paagrepet", False),
        ("https://www.bt.no/i/8p7y5w?pinnedEntryId=1", False),
        ("https://www.bt.no/i/8p7y5w?xpinnedEntry=1", False),
        ("https://www.bt.no/pinnedEntry=1/i/8p7y5w", False),
        (None, False),
        ("", False),
    ),
)
def test_is_page_anchor(url, expected):
    assert is_page_anchor(url) == expected


def test_two_kortnytt_on_one_page_share_a_join_key():
    """Why the anchor check exists rather than leaning on `normalize`.

    The fragment is not part of the key — correctly, it is not part of the
    request either — so these two unrelated stories are indistinguishable here.
    """
    page = "https://www.nrk.no/nordland/nyhetssenter-nordland-1.15898380"
    assert normalize(f"{page}#1.17992892.1.17992892") == normalize(
        f"{page}#1.17993014.1.17993014"
    )

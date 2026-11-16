"""Body rendering: what survives the trip from the scraper to the story page."""

import pytest

from radar.text import article_html, html_to_text, standfirst

LEDE = "Dette er en helt vanlig innledning som er lang nok til å telle som brødtekst i artikkelen."


def rail(headings: int) -> str:
    """A body ending in `headings` bare headings — the teaser-rail shape."""
    return LEDE + "\n\n" + "\n\n".join(f"## Sak nummer {i}" for i in range(headings))


# --- the trailing bare-heading run: cut at three, not two ---------------------


@pytest.mark.parametrize(
    "headings,expected_kept",
    (
        (0, 0),
        (1, 1),
        (2, 2),  # one below the limit — an article may end with two sections
        (3, 0),  # exactly at it
        (4, 0),  # above
    ),
)
def test_trailing_heading_run_cut_at_three(headings, expected_kept):
    html = article_html(rail(headings))
    assert html.count("<h2>") == expected_kept
    assert "vanlig innledning" in html


def test_heading_run_with_prose_between_is_kept():
    """Three headings are fine when the article is still reporting between them."""
    body = "\n\n".join([LEDE, "## En", LEDE, "## To", LEDE, "## Tre", LEDE])
    assert article_html(body).count("<h2>") == 3


# --- the labelled rail: only when no prose survives it ------------------------


@pytest.mark.parametrize("para_chars,rail_cut", ((109, True), (110, True), (111, False)))
def test_labelled_rail_needs_every_following_block_short(para_chars, rail_cut):
    """110 chars still reads as a deadline or caption; 111 is prose, so the story goes on."""
    body = "\n\n".join([LEDE, "## Mest lest", "### Direktør for Havdivisjonen", "a" * para_chars])
    html = article_html(body)
    assert ("Mest lest" not in html) is rail_cut
    assert ("a" * para_chars in html) is not rail_cut


def test_mid_article_rail_label_keeps_the_article():
    """forskning.no drops "Mest lest" into the middle of a story and keeps reporting."""
    body = "\n\n".join([LEDE, "## Mest lest", LEDE, "## Ikke krav om sterilisering", LEDE])
    html = article_html(body)
    assert "Mest lest" in html
    assert html.count("vanlig innledning") == 3


@pytest.mark.parametrize(
    "label",
    ("Mest lest", "Ledige stillinger", "LES OGSÅ", "Har du fått med deg disse klippene?"),
)
def test_rail_labels_are_matched_case_insensitively(label):
    body = "\n\n".join([LEDE, f"## {label}", "### Noe annet", "### Noe tredje"])
    assert label not in article_html(body)


def test_unlabelled_heading_before_short_blocks_is_kept():
    """Only known rail labels trigger the cut — a real subhead is not a rail."""
    body = "\n\n".join([LEDE, "## Kan smake hormoner", "### Detalj", "Kort svar."])
    html = article_html(body)
    assert "Kan smake hormoner" in html
    assert "Kort svar." in html


# --- empty bullets ------------------------------------------------------------


def test_empty_bullets_are_dropped_but_their_text_survives():
    """Faktisk's corrections log uses a bare "-" above real editorial text."""
    body = "\n\n".join([LEDE, "## Endringslogg", "-\nTidligere omtalte vi Pia Friis som sexolog."])
    html = article_html(body)
    assert "<li></li>" not in html
    assert "<ul>" not in html
    assert "Pia Friis" in html


def test_list_with_content_is_kept():
    html = article_html(LEDE + "\n\n- Første punkt\n- Andre punkt")
    assert html.count("<li>") == 2
    assert "Første punkt" in html


def test_partly_empty_list_keeps_the_real_items():
    html = article_html(LEDE + "\n\n-\n- Ekte punkt\n-")
    assert html.count("<li>") == 1
    assert "Ekte punkt" in html


# --- lede furniture -----------------------------------------------------------


@pytest.mark.parametrize("kicker_chars,is_furniture", ((69, True), (70, True), (71, False)))
def test_kicker_dropped_up_to_seventy_chars(kicker_chars, is_furniture):
    """A short line above the H1 is the source's own kicker, and the H1 repeats
    the title the page already prints. Past 70 chars it is a paragraph, so the
    heading below it is a section head and both stay."""
    kicker = "k" * kicker_chars
    html = article_html(f"{kicker}\n\n# Overskriften\n\n{LEDE}")
    assert (kicker in html) is not is_furniture
    assert ("Overskriften" in html) is not is_furniture


def test_leading_h1_dropped_without_a_kicker():
    html = article_html(f"# Overskriften\n\n{LEDE}")
    assert "Overskriften" not in html
    assert "vanlig innledning" in html


def test_later_h1_demoted_so_the_page_keeps_one_h1():
    html = article_html(f"{LEDE}\n\n# Mellomtittel\n\n{LEDE}")
    assert "<h1>" not in html
    assert "<h2>Mellomtittel</h2>" in html


# --- links, emphasis, safety --------------------------------------------------


def test_relative_link_resolved_against_the_article():
    html = article_html("Se [her](/nyheter/sak).", base_url="https://www.adressa.no/a/123")
    assert 'href="https://www.adressa.no/nyheter/sak"' in html


def test_absolute_link_left_alone_and_marked_external():
    html = article_html("Se [her](https://vg.no/x).", base_url="https://nrk.no/a/1")
    assert 'href="https://vg.no/x"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener nofollow"' in html


@pytest.mark.parametrize(
    "raw,expected",
    (
        ("**Aksjen faller. **", "<p><strong>Aksjen faller.</strong></p>\n"),
        ("** Aksjen faller.**", "<p><strong>Aksjen faller.</strong></p>\n"),
        ("**Aksjen faller.**", "<p><strong>Aksjen faller.</strong></p>\n"),
        # a lone asterisk is not emphasis and must survive untouched
        ("Prisen er 3*4 kroner", "<p>Prisen er 3*4 kroner</p>\n"),
    ),
)
def test_loose_bold_markers_are_closed(raw, expected):
    assert article_html(raw) == expected


@pytest.mark.parametrize(
    "raw",
    (
        "Tekst <img src=x onerror=alert(1)> her",
        "Tekst <script>alert(1)</script> her",
        "Tekst <iframe src=evil></iframe> her",
    ),
)
def test_markup_never_reaches_the_page(raw):
    html = article_html(raw)
    assert "<img" not in html
    assert "<script" not in html
    assert "<iframe" not in html
    assert "alert" not in html or "&lt;" in html


@pytest.mark.parametrize("value", (None, "", "   ", "\n\n"))
def test_empty_bodies_render_nothing(value):
    assert article_html(value) == ""


# --- html flattening ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    (
        ("<p>Hei &amp; hopp</p>", "Hei & hopp"),
        ("<p>En</p><p>To</p>", "En\n\nTo"),
        ("<p>Tekst</p><script>skjult()</script>", "Tekst"),
        ("Ren markdown passerer", "Ren markdown passerer"),
        (None, None),
        ("", ""),
    ),
)
def test_html_to_text(raw, expected):
    assert html_to_text(raw) == expected


# --- the furniture between the headline and the story --------------------------

FRIFAG_LEDE = "Nye retningslinjer møter en kald skulder blant lærere. De opplever det som skrivebordsarbeid uten skikkelige verktøy."

FRIFAG = f"""# Oppgitt over tiltak mot skolefravær: – Hva tror de vi driver med?

##### {FRIFAG_LEDE}

Nye retningslinjer mot skolefravær slaktes av flere lærere.

COLOURBOX/Lev Dolgachov

#### Saken oppsummert

journalist@example.no

Egne møter med eleven, en dedikert person på skolen som skal følge opp den enkelte elev og hjemmebesøk for å få elever til å møte opp på skolen er blant det som foreslås i den nye retningslinjen.

Tanken bak er god, men innholdet står nærmest til stryk, ifølge Hanne Mjøs Persen."""


def test_lede_furniture_run_is_removed():
    """Everything Frifagbevegelse prints before the first sentence of the story."""
    html = article_html(FRIFAG, summary=FRIFAG_LEDE)
    for junk in ("COLOURBOX", "Saken oppsummert", "journalist@example.no",
                 "slaktes av flere lærere", "kald skulder"):
        assert junk not in html
    assert "Egne møter med eleven" in html
    assert "Hanne Mjøs Persen" in html


def test_the_story_survives_when_there_is_no_furniture():
    body = "# Tittel\n\nEn helt vanlig åpning som er lang nok til å telle som brødtekst i artikkelen her.\n\nOg en til."
    html = article_html(body, summary="En annen ingress enn brødteksten.")
    assert "En helt vanlig åpning" in html
    assert "Og en til." in html


def test_a_short_opening_paragraph_is_not_mistaken_for_furniture():
    """The guard that keeps this from eating articles that just start briefly."""
    body = "# Tittel\n\n– Dette er uakseptabelt.\n\nSlik svarer statsråden på kritikken fra lærerne, som mener retningslinjene mangler verktøy."
    html = article_html(body)
    assert "Dette er uakseptabelt" in html


def test_furniture_is_only_stripped_near_the_top():
    """A slash-line deep in a long article is prose, and stays."""
    body = (
        "# Tittel\n\n"
        + "\n\n".join(f"Et helt vanlig avsnitt nummer {i} som er langt nok til å regnes som brødtekst her." for i in range(9))
        + "\n\nJa/nei-spørsmålet står igjen"
    )
    html = article_html(body)
    assert "Ja/nei-spørsmålet står igjen" in html


def test_summary_repeated_as_a_plain_paragraph_also_goes():
    body = f"# Tittel\n\n{FRIFAG_LEDE}\n\nSelve saken begynner her, og dette avsnittet er langt nok til å telle som ordentlig brødtekst."
    html = article_html(body, summary=FRIFAG_LEDE)
    assert "kald skulder" not in html
    assert "Selve saken begynner her" in html


def test_without_a_summary_the_standfirst_is_left_alone():
    """Nothing is guessed: the duplicate is only known because the page prints it."""
    body = f"# Tittel\n\n##### {FRIFAG_LEDE}\n\nSelve saken begynner her, og dette avsnittet er langt nok til å telle som ordentlig brødtekst."
    assert "kald skulder" in article_html(body)


# --- a summary that is really the whole article -------------------------------

# NRK files kortnytt with the entire brief in the feed's description and the
# same text again in the body.
KORTNYTT_LEDE = (
    "Kunnskapsminister Kari Nessa Nordtun (Ap) åpner for at norske elever kan få opplæring i "
    "krigsberedskap allerede fra neste år. Regjeringens omlegging av eksamen frigjør tid i juni. "
    "Elevene skal vurderes med «bestått» eller «ikke bestått». (NTB)"
)
KORTNYTT_BODY = (
    "## Krigsberedskap kan komme på timeplanen neste år\n\n"
    "Kunnskapsminister Kari Nessa Nordtun (Ap) åpner for at norske elever kan få opplæring i "
    "krigsberedskap allerede fra neste år.\n\n"
    "Regjeringens omlegging av eksamen frigjør tid i juni, og Nordtun ser for seg at denne tiden "
    "kan brukes til et kurs om beredskap og verneplikt.\n\n"
    "Elevene skal vurderes med «bestått» eller «ikke bestått»."
)


@pytest.mark.parametrize(
    "lede_chars,is_standfirst",
    (
        (99, True),  # just under half the body — a teaser
        (100, False),  # exactly half
        (101, False),  # over
    ),
)
def test_standfirst_gives_up_at_half_the_body(lede_chars, is_standfirst):
    kept = standfirst("a" * lede_chars, "b" * 200)
    assert (kept is not None) is is_standfirst


@pytest.mark.parametrize("summary", (None, "", "   "), ids=["none", "empty", "blank"])
def test_standfirst_of_nothing_is_nothing(summary):
    assert standfirst(summary, "b" * 200) is None


@pytest.mark.parametrize("body", (None, "", "   "), ids=["none", "empty", "blank"])
def test_standfirst_survives_a_body_we_could_not_read(body):
    """No body to measure against is no reason to throw the lede away."""
    assert standfirst("En helt vanlig ingress.", body) == "En helt vanlig ingress."


def test_kortnytt_summary_is_not_printed_as_a_lede():
    assert standfirst(KORTNYTT_LEDE, KORTNYTT_BODY) is None


def test_kortnytt_body_keeps_its_opening_paragraph():
    """The body is the better copy — it has the paragraph breaks and the links
    the feed's description flattened away — so nothing of it may be dropped as
    a duplicate of a lede the page is no longer printing."""
    html = article_html(KORTNYTT_BODY, summary=None, title="Krigsberedskap kan komme på timeplanen neste år")
    assert "Kunnskapsminister Kari Nessa Nordtun" in html
    assert "ikke bestått" in html


def test_body_heading_that_repeats_the_title_goes():
    html = article_html(KORTNYTT_BODY, title="Krigsberedskap kan komme på timeplanen neste år")
    assert "<h2>" not in html


def test_body_heading_that_is_a_real_subhead_stays():
    """Only the headline the page already prints is furniture."""
    html = article_html(KORTNYTT_BODY, title="En helt annen tittel enn den i brødteksten")
    assert "<h2>Krigsberedskap kan komme på timeplanen neste år</h2>" in html


# --- a standfirst the prose guard used to hide behind --------------------------

STORY = "Selve saken begynner her, og dette avsnittet er langt nok til å telle som ordentlig brødtekst."


@pytest.mark.parametrize("lede_chars", (199, 200, 201, 290))
def test_a_standfirst_goes_however_long_it_is(lede_chars):
    """The guard that ends the furniture scan is a length test, and a long
    standfirst used to trip it — the scan stopped on the very block it was
    looking for, and the page printed the lede twice."""
    lede = "L" * (lede_chars - 1) + "."
    html = article_html(f"# Tittel\n\n{lede}\n\n{STORY}", summary=lede)
    assert lede not in html
    assert "Selve saken begynner her" in html


def test_furniture_above_the_standfirst_is_still_in_scope():
    """Dagens Medisin opens with a photo caption long enough to read as prose,
    and the standfirst sits a block below it."""
    caption = "PERSONTILPASSET: " + "c" * 220
    lede = "Norsk forskning viser at mer tilpasset behandling kan være bedre for pasientene."
    html = article_html(f"{caption}\n\n## Tittelen igjen\n\n{lede}\n\n{STORY}", summary=lede)
    assert lede not in html
    assert "Selve saken begynner her" in html


def test_the_guard_still_stops_the_scan_below_the_standfirst():
    """Past the standfirst the length test does its original job: real prose
    ends the run, and a credit line further down is not furniture any more."""
    lede = "En kort ingress."
    body = "\n\n".join([lede, "b" * 201, "Foto: Ola Nordmann"])
    html = article_html(f"# Tittel\n\n{body}", summary=lede)
    assert lede not in html
    assert "Foto: Ola Nordmann" in html

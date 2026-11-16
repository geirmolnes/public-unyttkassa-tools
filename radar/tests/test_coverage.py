"""Has Utdanningsnytt already written this? The ranking, the prompt, the answer."""

import pytest

from radar import coverage

ARTICLE = {
    "id": "NRK:1.17970004",
    "source": "NRK",
    "title": "Regjeringen snur om skolelydbøker for elever med dysleksi",
    "summary": "Statped-ordningen består likevel til høsten.",
    "topics": ["dysleksi", "lydbøker"],
    "body_excerpt": "Z" * 5000,  # Z appears nowhere in the prompt scaffolding
    "published_day": "2026-08-05",
}


def ours(title, *, day="2026-08-05", lead="", body="", keywords="", article_id=1):
    return {"id": article_id, "url": f"https://utdanningsnytt.no/{article_id}",
            "title": title, "day": day, "lead": lead, "body": body, "keywords": keywords}


def call(monkeypatch, *, article=ARTICLE, candidates=None, answer=None):
    """Run check() with the model stubbed. Returns (result, what_was_sent)."""
    if candidates is None:
        candidates = [ours("Nordtun snur i lydbok-saken")]
    sent = {}

    def fake_generate_json(system_prompt, schema, contents):
        sent.update(system=system_prompt, schema=schema, contents=contents)
        return answer if answer is not None else {"match": "INGEN", "begrunnelse": ""}

    monkeypatch.setattr(coverage.gemini, "generate_json", fake_generate_json)
    return coverage.check(article, candidates), sent


# --- which of our articles make the prompt ------------------------------------


def test_shortlist_ranks_the_shared_event_above_the_shared_theme():
    """Both candidates are about school. Only one is about this event."""
    candidates = [
        ours("Slik jobber skolen med lesing", lead="Lærere i skolen deler erfaringer"),
        ours("Nordtun snur: elever beholder lydbøkene", lead="Dysleksi-ordningen består"),
    ]
    ranked = coverage.shortlist(ARTICLE, candidates, 1)
    assert ranked[0]["title"].startswith("Nordtun snur")


def test_shortlist_discounts_words_that_are_in_everything_we_write():
    """Every candidate says «skole»; only one says «dysleksi». Rarity decides.

    Without the idf weighting the two long school-heavy candidates outscore the
    short one that actually matches, because they simply contain more words.
    """
    candidates = [
        ours("Skolen og skoleåret: skole i endring", lead="Skole, skolen, skoleledere, skolestart", article_id=1),
        ours("Skole og skolemat i skolen", lead="Skole, skolen, skoleeier, skolebibliotek", article_id=2),
        ours("Dysleksi-ordningen består", lead="", article_id=3),
    ]
    ranked = coverage.shortlist(ARTICLE, candidates, 1)
    assert ranked[0]["id"] == 3


def test_shortlist_caps_the_list():
    candidates = [ours(f"Sak {i}", article_id=i) for i in range(30)]
    assert len(coverage.shortlist(ARTICLE, candidates, 20)) == 20


def test_shortlist_falls_back_to_recency_without_anything_to_rank_on():
    bare = {"title": "", "summary": "", "topics": []}
    candidates = [ours(f"Sak {i}", article_id=i) for i in range(5)]
    assert [c["id"] for c in coverage.shortlist(bare, candidates, 2)] == [0, 1]


# --- the prompt ---------------------------------------------------------------


def test_prompt_carries_our_headlines_and_the_article(monkeypatch):
    _, sent = call(monkeypatch, candidates=[ours("Nordtun snur i lydbok-saken", day="2026-08-04")])
    assert "U1. 2026-08-04 · Nordtun snur i lydbok-saken" in sent["contents"]
    assert ARTICLE["title"] in sent["contents"]


def test_body_is_cut_to_the_lede(monkeypatch):
    from radar import config

    _, sent = call(monkeypatch)
    assert sent["contents"].count("Z") == config.COVERAGE_BODY_CHARS


def test_schema_offers_every_handle_and_the_way_out(monkeypatch):
    _, sent = call(monkeypatch, candidates=[ours("A", article_id=1), ours("B", article_id=2)])
    assert sent["schema"]["properties"]["match"]["enum"] == ["U1", "U2", "INGEN"]


# --- the answer ---------------------------------------------------------------


def test_a_handle_comes_back_as_a_match(monkeypatch):
    result, _ = call(monkeypatch, answer={"match": "U1", "begrunnelse": "Samme omgjøring."})
    assert result.handle == "U1"
    assert result.rationale == "Samme omgjøring."
    assert not result.is_none


def test_ingen_is_a_real_answer_not_an_error(monkeypatch):
    result, _ = call(monkeypatch, answer={"match": "INGEN", "begrunnelse": ""})
    assert result.is_none
    assert result.error is None
    assert result.rationale is None


def test_an_unknown_handle_is_an_error_not_a_match(monkeypatch):
    """The direction that matters: never invent coverage we can't point at."""
    result, _ = call(monkeypatch, answer={"match": "U9", "begrunnelse": "Jo da."})
    assert result.error and result.handle is None


def test_html_entities_in_the_rationale_are_unescaped(monkeypatch):
    result, _ = call(
        monkeypatch, answer={"match": "U1", "begrunnelse": "Samme sak om l&aelig;rerne."}
    )
    assert result.rationale == "Samme sak om lærerne."


def test_a_failed_call_comes_back_as_an_error(monkeypatch):
    def boom(*_):
        raise coverage.gemini.GeminiError("429 quota")

    monkeypatch.setattr(coverage.gemini, "generate_json", boom)
    result = coverage.check(ARTICLE, [ours("A")])
    assert result.error == "429 quota"
    assert result.handle is None


def test_no_candidates_means_no_call(monkeypatch):
    def boom(*_):
        raise AssertionError("should not have called the model")

    monkeypatch.setattr(coverage.gemini, "generate_json", boom)
    assert coverage.check(ARTICLE, []).is_none


@pytest.mark.parametrize("answer", ({"match": "", "begrunnelse": ""}, {}))
def test_a_blank_answer_is_an_error(monkeypatch, answer):
    result, _ = call(monkeypatch, answer=answer)
    assert result.error


# --- covered, and still worth reading -----------------------------------------


def followup(match="U1", *, nytt=True, nytt_er="Lærerutdannerne svarer for første gang."):
    return {"match": match, "begrunnelse": "Samme forslag om ekspertpanel.",
            "nytt": nytt, "nytt_er": nytt_er}


def test_a_covered_article_can_still_bring_something_new(monkeypatch):
    """The case this exists for: a reaction to an event we already wrote about."""
    result, _ = call(monkeypatch, answer=followup())
    assert result.handle == "U1"
    assert result.whats_new == "Lærerutdannerne svarer for første gang."
    assert result.is_followup


@pytest.mark.parametrize(
    "nytt,nytt_er,expected",
    (
        (True, "Nye tall fra Udir.", "Nye tall fra Udir."),
        (True, "", None),  # a flag with nothing to show is not a follow-up
        (True, "   ", None),
        (False, "Nye tall fra Udir.", None),  # the flag wins over a stray sentence
        (False, "", None),
    ),
)
def test_both_halves_are_needed_before_it_counts(monkeypatch, nytt, nytt_er, expected):
    result, _ = call(monkeypatch, answer=followup(nytt=nytt, nytt_er=nytt_er))
    assert result.whats_new == expected
    assert result.is_followup is (expected is not None)


def test_nothing_of_ours_matches_so_nothing_is_a_follow_up(monkeypatch):
    """«INGEN» outranks the second question — there is no story to follow up."""
    result, _ = call(monkeypatch, answer=followup("INGEN"))
    assert result.is_none
    assert not result.is_followup
    assert result.whats_new is None  # never stored against a row that matched nothing


def test_the_schema_requires_both_halves(monkeypatch):
    _, sent = call(monkeypatch)
    props = sent["schema"]["properties"]
    assert props["nytt"]["type"] == "BOOLEAN"
    assert sent["schema"]["required"] == ["match", "begrunnelse", "nytt", "nytt_er"]


def test_html_entities_in_the_new_line_are_unescaped(monkeypatch):
    answer = followup(nytt_er="UHR l&aelig;rerutdanning avviser forslaget.")
    result, _ = call(monkeypatch, answer=answer)
    assert result.whats_new == "UHR lærerutdanning avviser forslaget."


def test_an_older_answer_without_the_new_fields_is_still_read(monkeypatch):
    """Rows written by prompt d1 are re-asked, not crashed on."""
    result, _ = call(monkeypatch, answer={"match": "U1", "begrunnelse": "Samme vedtak."})
    assert result.handle == "U1"
    assert result.whats_new is None
    assert not result.is_followup


# --- lines we refuse to print -------------------------------------------------


@pytest.mark.parametrize(
    "line,kept",
    (
        ("Lærerutdannerne avviser forslaget.", True),
        ("Elevene vurderes med «bestått» — eller ikke.", True),  # quotes, en dash
        ("Nye tall: 1 av 3 skoler, ifølge Udir.", True),  # digits and punctuation
        ("Ny rapport fra Sørøst-Norge (USN).", True),
        ("Détaljer om Müller-saken.", True),  # accented Latin is still Latin
        ("Detaljer om lær器的 forklaring i saken.", False),  # seen in a real run
        ("Подробности о деле.", False),
        ("説明。", False),
    ),
)
def test_a_line_in_the_wrong_script_is_dropped_whole(monkeypatch, line, kept):
    """Gemini spliced two CJK ideographs into a Norwegian word. Half a garbled
    sentence is worse than none — the desk acts on this line or it shouldn't
    be there — so the line goes and the row falls back to plain «dekket»."""
    result, _ = call(monkeypatch, answer=followup(nytt_er=line))
    assert result.whats_new == (line if kept else None)
    assert result.is_followup is kept


def test_a_garbled_rationale_goes_the_same_way(monkeypatch):
    answer = {"match": "U1", "begrunnelse": "Samme 器的 vedtak.", "nytt": False, "nytt_er": ""}
    result, _ = call(monkeypatch, answer=answer)
    assert result.rationale is None
    assert result.handle == "U1"  # the verdict survives; only the prose is dropped


# --- which candidate a handle points at ---------------------------------------


@pytest.mark.parametrize(
    "match,expected_id",
    (
        (coverage.Match(handle="U1"), 1),
        (coverage.Match(handle="U3"), 3),
        (coverage.Match(handle="INGEN"), None),
        (coverage.Match(error="429 quota"), None),  # nothing to point at
    ),
)
def test_matched_resolves_the_handle(match, expected_id):
    candidates = [ours(f"Sak {i}", article_id=i) for i in (1, 2, 3)]
    got = coverage.matched(candidates, match)
    assert (got["id"] if got else None) == expected_id


# --- ranking our own articles by their text -----------------------------------

# The claim that started this: our own piece carried the quote 3800 characters
# in, under a headline about something else entirely.
YSTEBO = "KrF-politiker Joel Ystebø kaller forslaget om lengre skoledag galskap."


def test_rank_by_body_finds_the_text_the_headline_hides():
    """The whole point of a second ranking: shortlist reads ledes, this reads text.

    Candidate 2 is the real one — its title and lede are about a school
    settlement, and the quote sits three thousand characters into the body.
    """
    candidates = [
        ours("Skoledagen kan bli lengre", lead="Forslaget om lengre skoledag",
             body="Forslaget om lengre skoledag kommer fra utvalget.", article_id=1),
        ours("Høyre og Frp avviser ikke et bredt skoleforlik",
             lead="Fellesskoleutvalget etterlyser et bredt forlik.",
             body="X" * 3000 + " Joel Ystebø kaller forslaget galskap.", article_id=2),
    ]
    assert [c["id"] for c in coverage.rank_by_body(YSTEBO, candidates, 2)] == [2, 1]


def test_rank_by_body_discounts_words_that_are_in_everything_we_write():
    """Same idf reasoning as shortlist, over a different field.

    The runner-up shares four words with the claim and the winner two, so the
    rarity weighting is the only thing that can decide this — everything we
    publish says «skoledag» and «forslaget», and one piece says «Ystebø».
    """
    candidates = [
        *(ours("Bakgrunn", body="forslaget om lengre skoledag fra en politiker",
               article_id=i) for i in range(10)),
        ours("Fire vanlige ord", body="forslaget om lengre skoledag, sier en politiker",
             article_id=98),
        ours("To sjeldne ord", body="Ystebø kaller det galskap.", article_id=99),
    ]
    assert coverage.rank_by_body(YSTEBO, candidates, 1)[0]["id"] == 99


@pytest.mark.parametrize("limit,expected", ((1, 1), (4, 4), (5, 5), (6, 5)))
def test_rank_by_body_caps_the_list(limit, expected):
    candidates = [ours(f"Sak {i}", body="Ystebø galskap", article_id=i) for i in range(5)]
    assert len(coverage.rank_by_body(YSTEBO, candidates, limit)) == expected


def test_rank_by_body_returns_only_what_actually_overlaps():
    """A slot left empty rather than filled with an article sharing nothing.

    This is what makes the pass free on a claim about a name we never printed.
    """
    candidates = [
        ours("A", body="Barnehagene mangler vikarer.", article_id=1),
        ours("B", body="Ystebø kaller det galskap.", article_id=2),
        ours("C", body="Eksamen avlyses i Bergen.", article_id=3),
    ]
    assert [c["id"] for c in coverage.rank_by_body(YSTEBO, candidates, 3)] == [2]


def test_rank_by_body_breaks_ties_on_the_order_it_was_given():
    candidates = [ours(f"Sak {i}", body="Ystebø galskap", article_id=i) for i in range(3)]
    assert [c["id"] for c in coverage.rank_by_body(YSTEBO, candidates, 2)] == [0, 1]


@pytest.mark.parametrize(
    "line,body,expected",
    (
        ("Rødt varsler omkamp om SFO.", "Rødt stemte imot i salen.", 1),  # 4 letters
        ("KrF varsler omkamp om SFO.", "KrF stemte imot i salen.", 0),  # 3, and skipped
    ),
)
def test_rank_by_body_ignores_tokens_under_four_characters(line, body, expected):
    """«KrF» cannot carry a match on its own — the same rule the module opens with."""
    assert len(coverage.rank_by_body(line, [ours("A", body=body)], 3)) == expected


@pytest.mark.parametrize(
    "line,candidates",
    (
        ("", "one"),  # nothing to look for
        ("   ", "one"),
        (YSTEBO, "none"),  # nothing to look in
    ),
)
def test_rank_by_body_has_nothing_to_say_without_both_halves(line, candidates):
    pool = [ours("A", body="Ystebø galskap")] if candidates == "one" else []
    assert coverage.rank_by_body(line, pool, 3) == []


# --- is what looks new already ours? ------------------------------------------


def novelty(monkeypatch, *, whats_new=YSTEBO, pool=None, exclude=None, answer=None, fail=None):
    """Run verify_novelty with the model stubbed. Returns (result, what_was_sent)."""
    if pool is None:
        pool = [ours("Vår sak", body="Ystebø kaller forslaget galskap.", article_id=7)]
    sent = {}

    def fake_generate_json(system_prompt, schema, contents):
        sent.update(system=system_prompt, schema=schema, contents=contents)
        if fail:
            raise coverage.gemini.GeminiError(fail)
        return answer if answer is not None else {"finnes": "INGEN"}

    monkeypatch.setattr(coverage.gemini, "generate_json", fake_generate_json)
    match = coverage.Match(handle="U1", rationale="Samme utvalg.", whats_new=whats_new)
    return coverage.verify_novelty(match, pool, exclude=exclude), sent


def test_a_claim_we_already_printed_stops_being_new(monkeypatch):
    """The Ystebø case: covered by us, and his reaction was in a piece of ours too."""
    result, _ = novelty(monkeypatch, answer={"finnes": "V1"})
    assert result.whats_new is None
    assert not result.is_followup
    assert result.handle == "U1"  # still covered — only the second half changed
    assert result.rationale == "Samme utvalg."


@pytest.mark.parametrize(
    "answer,fail",
    (
        ({"finnes": "INGEN"}, None),  # we really don't have it
        ({"finnes": "V9"}, None),  # out of range: never trusted either way
        ({}, None),
        ({"finnes": ""}, None),
        (None, "429 quota"),  # the model never answered
    ),
)
def test_anything_short_of_a_hit_leaves_the_claim_standing(monkeypatch, answer, fail):
    """The doubt rule, inverted: suppressing a real follow-up costs the story."""
    result, _ = novelty(monkeypatch, answer=answer, fail=fail)
    assert result.whats_new == YSTEBO
    assert result.is_followup


@pytest.mark.parametrize("whats_new", (None, "", "   "))
def test_nothing_claimed_means_no_call(monkeypatch, whats_new):
    result, sent = novelty(monkeypatch, whats_new=whats_new, answer={"finnes": "V1"})
    assert sent == {}
    assert result.whats_new == whats_new


def test_nothing_of_ours_overlaps_so_no_call(monkeypatch):
    pool = [ours("Vår sak", body="Barnehagene mangler vikarer.", article_id=7)]
    result, sent = novelty(monkeypatch, pool=pool, answer={"finnes": "V1"})
    assert sent == {}
    assert result.whats_new == YSTEBO


def test_the_article_already_compared_against_is_left_out(monkeypatch):
    """Pass 1 read that one and said it lacked this. Asking again wastes a slot."""
    already = ours("Vår sak", body="Ystebø kaller forslaget galskap.", article_id=7)
    result, sent = novelty(
        monkeypatch, pool=[already], exclude=already, answer={"finnes": "V1"}
    )
    assert sent == {}
    assert result.whats_new == YSTEBO


def test_the_prompt_carries_the_claim_and_our_whole_text(monkeypatch):
    """Not the lede — the lede is what failed to answer this in the first place."""
    pool = [ours("Vår sak", day="2026-08-11", lead="Om skoleforliket",
                 body="Langt inni: Ystebø kaller forslaget galskap.", article_id=7)]
    _, sent = novelty(monkeypatch, pool=pool)
    assert YSTEBO in sent["contents"]
    assert "V1. 2026-08-11 · Vår sak" in sent["contents"]
    assert "Langt inni: Ystebø kaller forslaget galskap." in sent["contents"]


def test_the_schema_offers_every_handle_and_the_way_out(monkeypatch):
    pool = [ours(f"Sak {i}", body="Ystebø galskap", article_id=i) for i in (1, 2)]
    _, sent = novelty(monkeypatch, pool=pool)
    assert sent["schema"]["properties"]["finnes"]["enum"] == ["V1", "V2", "INGEN"]
    assert sent["schema"]["required"] == ["finnes"]


def test_only_the_best_few_of_ours_reach_the_prompt(monkeypatch):
    """They go in whole, so the list is short where the match question's is long."""
    monkeypatch.setattr(coverage.config, "COVERAGE_NOVELTY_CANDIDATES", 2)
    pool = [ours(f"Sak {i}", body="Ystebø galskap", article_id=i) for i in (1, 2, 3)]
    _, sent = novelty(monkeypatch, pool=pool)
    assert "V2." in sent["contents"]
    assert "V3." not in sent["contents"]

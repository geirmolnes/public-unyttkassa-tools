"""Tying articles to running stories: the prompt we send and the answer we trust."""

import pytest

from radar import cluster
from radar.web.app import other_sources_label

CANDIDATES = [
    {
        "id": "abc123",
        "label": "Mobilforbud i grunnskolen",
        "first_day": "2026-07-22",
        "last_day": "2026-08-06",
        "n_articles": 11,
        "titles": ["Regjeringen vil lovfeste mobilforbud", "KS advarer mot detaljstyring"],
    },
    {
        "id": "def456",
        "label": "Bemanningsnorm i barnehagene",
        "first_day": "2026-08-01",
        "last_day": "2026-08-01",
        "n_articles": 2,
        "titles": [],
    },
]

ARTICLE = {
    "id": "NRK:1.1",
    "source": "NRK",
    "title": "Mobilforbudet blir lov fra nyttår",
    "summary": "Stortinget vedtok forslaget tirsdag.",
    "body_excerpt": "Z" * 5000,  # Z appears nowhere in the prompt scaffolding
    "published_day": "2026-08-06",
}


def call(monkeypatch, *, article=ARTICLE, candidates=CANDIDATES, answer=None):
    """Run assign() with the model stubbed. Returns (result, what_was_sent)."""
    sent = {}

    def fake_generate_json(system_prompt, schema, contents):
        sent.update(system=system_prompt, schema=schema, contents=contents)
        return answer if answer is not None else {"cluster_id": "NY", "label": "En sak"}

    monkeypatch.setattr(cluster.gemini, "generate_json", fake_generate_json)
    return cluster.assign(article, candidates), sent


# --- which candidates make the prompt -----------------------------------------


def story(label, topics, **extra):
    return {"id": label, "label": label, "topics": topics, **extra}


def test_shortlist_keeps_everything_when_it_already_fits():
    stories = [story("A", ["x"]), story("B", ["y"])]
    assert cluster.shortlist(["x"], stories, 5) == stories


def test_shortlist_puts_the_best_overlap_first():
    stories = [story("Ingen", []), story("Én", ["mobil"]), story("To", ["mobil", "skole"])]
    ranked = cluster.shortlist(["mobil", "skole"], stories, 2)
    assert [s["label"] for s in ranked] == ["To", "Én"]


def test_shortlist_rescues_a_quiet_story_from_below_the_cut():
    """The whole point: recency alone would have dropped this one."""
    stories = [story(f"Fersk {i}", ["annet"]) for i in range(5)]
    stories.append(story("Stille sak", ["mobilforbud"]))
    ranked = cluster.shortlist(["mobilforbud"], stories, 2)
    assert ranked[0]["label"] == "Stille sak"


def test_shortlist_prefers_a_close_fit_to_a_broad_story():
    """The snowball: a story's topics are the union of every article in it, so
    the biggest story shares topics with everything on the beat. Ranked by raw
    overlap it wins every shortlist, gets picked more, and grows into a theme."""
    theme = story("Kunnskapsløft og skolepolitikk", [f"t{i}" for i in range(40)] + ["nou", "skole"])
    precise = story("NOU om framtidas skole", ["nou", "skole", "fellesskoleutvalget"])
    ranked = cluster.shortlist(["nou", "skole", "fellesskoleutvalget"], [theme, precise], 1)
    assert [s["label"] for s in ranked] == ["NOU om framtidas skole"]


def test_shortlist_still_prefers_more_overlap_at_equal_breadth():
    """Narrowing the ranking must not stop it ranking: between two stories of the
    same size, the one sharing more still wins."""
    stories = [story("Én", ["mobil", "annet"]), story("To", ["mobil", "skole"])]
    ranked = cluster.shortlist(["mobil", "skole"], stories, 1)
    assert [s["label"] for s in ranked] == ["To"]


def test_shortlist_ties_keep_the_incoming_recency_order():
    stories = [story("Fersk", ["mobil"]), story("Eldre", ["mobil"])]
    ranked = cluster.shortlist(["mobil"], stories, 1)
    assert [s["label"] for s in ranked] == ["Fersk"]


@pytest.mark.parametrize("topics", (None, [], ["  "]), ids=["none", "empty", "blank"])
def test_shortlist_without_topics_falls_back_to_recency(topics):
    stories = [story(f"S{i}", ["mobil"]) for i in range(4)]
    ranked = cluster.shortlist(topics, stories, 2)
    assert [s["label"] for s in ranked] == ["S0", "S1"]


def test_shortlist_matches_topics_case_insensitively():
    stories = [story("Annet", ["skole"]), story("Treff", ["Mobilforbud"])]
    ranked = cluster.shortlist(["mobilforbud"], stories, 1)
    assert ranked[0]["label"] == "Treff"


def test_shortlist_tolerates_a_story_with_no_topics():
    stories = [story("Uten", None), story("Med", ["mobil"])]
    ranked = cluster.shortlist(["mobil"], stories, 1)
    assert ranked[0]["label"] == "Med"


# --- what the model is shown --------------------------------------------------


def test_candidates_are_numbered_from_one(monkeypatch):
    """The model answers by handle, so S1 must be the first candidate."""
    _, sent = call(monkeypatch)
    assert "S1. Mobilforbud i grunnskolen" in sent["contents"]
    assert "S2. Bemanningsnorm i barnehagene" in sent["contents"]


@pytest.mark.parametrize(
    "fragment",
    (
        "(2026-07-22 til 2026-08-06, 11 artikler)",  # multi-day span reads as a range
        "(2026-08-01, 2 artikler)",  # a one-day story shows one date, not "x til x"
        "    - KS advarer mot detaljstyring",
    ),
)
def test_candidate_lines_carry_span_and_titles(monkeypatch, fragment):
    _, sent = call(monkeypatch)
    assert fragment in sent["contents"]


def test_prompt_says_so_when_nothing_is_running(monkeypatch):
    """The very first article has no candidates and must still get a usable prompt."""
    _, sent = call(monkeypatch, candidates=[])
    assert "ingen — dette blir den første saken" in sent["contents"]


def test_prompt_omits_absent_fields(monkeypatch):
    bare = {"id": "VG:1", "source": "VG", "title": "Kort sak", "published_day": "2026-08-06"}
    _, sent = call(monkeypatch, article=bare)
    assert "Ingress:" not in sent["contents"]
    assert "Brødtekst:" not in sent["contents"]


@pytest.mark.parametrize("length,expected", ((99, 99), (100, 100), (101, 100)))
def test_body_is_cut_at_the_configured_limit(monkeypatch, length, expected):
    monkeypatch.setattr(cluster.config, "CLUSTER_BODY_CHARS", 100)
    article = {**ARTICLE, "body_excerpt": "Z" * length}
    _, sent = call(monkeypatch, article=article)
    assert sent["contents"].count("Z") == expected


# --- the response schema pins the answer --------------------------------------


@pytest.mark.parametrize(
    "n_candidates,expected_enum",
    (
        (0, ["NY"]),  # nothing to join: the only legal answer is a new story
        (1, ["S1", "NY"]),
        (2, ["S1", "S2", "NY"]),
    ),
)
def test_schema_offers_exactly_the_handles_on_the_list(monkeypatch, n_candidates, expected_enum):
    _, sent = call(monkeypatch, candidates=CANDIDATES[:n_candidates])
    assert sent["schema"]["properties"]["cluster_id"]["enum"] == expected_enum


# --- reading the answer -------------------------------------------------------


def test_assign_accepts_a_handle_on_the_list(monkeypatch):
    answer = {"cluster_id": "S1", "label": "Mobilforbud i grunnskolen"}
    result, _ = call(monkeypatch, answer=answer)
    assert (result.handle, result.label, result.is_new, result.error) == (
        "S1",
        "Mobilforbud i grunnskolen",
        False,
        None,
    )


def test_assign_accepts_the_last_handle_on_the_list(monkeypatch):
    """Off-by-one guard: S2 of two candidates is valid, not one past the end."""
    result, _ = call(monkeypatch, answer={"cluster_id": "S2", "label": "Bemanningsnorm"})
    assert result.handle == "S2"
    assert result.error is None


def test_assign_accepts_a_new_story(monkeypatch):
    result, _ = call(monkeypatch, answer={"cluster_id": "NY", "label": "Streik i barnehagene"})
    assert (result.is_new, result.label, result.error) == (True, "Streik i barnehagene", None)


@pytest.mark.parametrize(
    "handle",
    ("S3", "S0", "", "abc123", "ny"),
    ids=["one-past-end", "zero", "empty", "raw-id", "wrong-case"],
)
def test_assign_rejects_a_handle_off_the_list(monkeypatch, handle):
    """A stray handle is an error, never a silently-founded new story — that
    would fragment the very continuity the feature exists to show."""
    result, _ = call(monkeypatch, answer={"cluster_id": handle, "label": "Noe"})
    assert result.handle is None
    assert result.error is not None


def test_assign_tolerates_whitespace_around_the_handle(monkeypatch):
    result, _ = call(monkeypatch, answer={"cluster_id": " S1 ", "label": " Mobilforbud "})
    assert (result.handle, result.label) == ("S1", "Mobilforbud")


def test_every_pass_offers_a_new_story(monkeypatch):
    _, sent = call(monkeypatch)
    assert sent["schema"]["properties"]["cluster_id"]["enum"] == ["S1", "S2", "NY"]


def test_assign_unescapes_entities_in_the_label(monkeypatch):
    """Seen in a real run: the model spelled æ as an HTML entity."""
    result, _ = call(
        monkeypatch, answer={"cluster_id": "NY", "label": "Ny leder i L&aelig;ringsverkstedet"}
    )
    assert result.label == "Ny leder i Læringsverkstedet"


def test_assign_rejects_a_new_story_without_a_label(monkeypatch):
    result, _ = call(monkeypatch, answer={"cluster_id": "NY", "label": "   "})
    assert result.error == "new story without a label"


def test_assign_reports_a_failed_call(monkeypatch):
    def boom(*args, **kwargs):
        raise cluster.gemini.GeminiError("429 rate limited")

    monkeypatch.setattr(cluster.gemini, "generate_json", boom)
    result = cluster.assign(ARTICLE, CANDIDATES)
    assert result.handle is None
    assert "429" in result.error


# --- what the feed says -------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    (
        (0, ""),
        (None, ""),
        (1, "Én annen kilde"),  # the singular is the whole reason this isn't an f-string
        (2, "2 andre kilder"),
        (11, "11 andre kilder"),
        (-1, ""),  # a count can't go negative, but the badge must not render "-1"
    ),
)
def test_other_sources_label(n, expected):
    assert other_sources_label(n) == expected

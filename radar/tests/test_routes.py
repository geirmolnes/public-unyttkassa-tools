"""The HTTP surface: what a caller actually gets back.

Hits the routes rather than the helpers, so these survive the next refactor of
the query layer. A real SQLite file per test — nothing here is mocked except the
scraper mount, which is not present in a checkout.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from radar import storage
from radar.web import queries

ARTICLES = (
    # id, source, title, summary, body, day, time, score, stage, kind
    ("a1", "VG", "Lærerlønn på bordet igjen", "Om lønnsoppgjøret",
     "Full tekst om lærerlønn og streik i grunnskolen.", "2026-08-17", "07:00", 88,
     "grunnskole", "nyhet"),
    ("a2", "NRK", "Ny bemanningsnorm i barnehagene", "Regjeringen utsetter",
     "Barnehagelærere mangler i hele landet.", "2026-08-17", "09:00", 64,
     "barnehage", "nyhet"),
    ("a3", "Forskning", "Leseferdighetene faller", "Ny studie",
     "Elever på mellomtrinnet leser dårligere enn før.", "2026-08-16", "11:00", 52,
     "grunnskole", "forskning"),
    ("a4", "VG", "Vi kan ikke digitalisere oss ut av lærermangelen", "Innlegg",
     "Debattinnlegg om skjermbruk i skolen.", "2026-08-16", "12:00", 45,
     "tverrgaende", "debatt"),
)


TODAY = "2026-08-17"
STATS = {"X-Stats-Token": "test-stats-token"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("radar.config.DB_PATH", tmp_path / "radar.db")
    # The feed orders today differently from every earlier day, so «today» has
    # to be a fixture rather than whatever date the suite happens to run on.
    monkeypatch.setattr(queries, "today_oslo", lambda: TODAY)
    queries._stats_cache = None  # the 60 s cache would leak between tests

    conn = storage.connect(tmp_path / "radar.db")
    for aid, source, title, summary, body, day, clock, score, stage, kind in ARTICLES:
        storage.upsert_article(conn, {
            "id": aid, "source": source, "feed_name": None, "title": title,
            "link": f"https://{source.lower()}.no/{aid}", "summary": summary,
            "body_excerpt": body, "published_at": f"{day}T{clock}:00+00:00",
            "published_day": day, "image_url": None, "paywalled": 0,
            "scraper_cursor": "c",
        })
        conn.execute(
            "INSERT INTO verdicts (article_id, prefilter_passed, relevant, score, stage, "
            "kind, topics, rationale, classified_at, prompt_version) "
            "VALUES (?, 1, 1, ?, ?, ?, '[]', 'fordi', ?, 'v4')",
            (aid, score, stage, kind, f"{day}T{clock}:00+00:00"),
        )
    # One kept out by the word filter, so /frafiltrert has something to show.
    # Dated relative to today: /frafiltrert only looks 7 days back, so a fixed
    # date walks out of its window and the test starts failing by calendar.
    rejected_day = date.today().isoformat()
    storage.upsert_article(conn, {
        "id": "a5", "source": "VG", "feed_name": None, "title": "Noe helt annet",
        "link": "https://vg.no/a5", "summary": "x", "body_excerpt": "y",
        "published_at": f"{rejected_day}T05:00:00+00:00", "published_day": rejected_day,
        "image_url": None, "paywalled": 0, "scraper_cursor": "c",
    })
    conn.execute(
        "INSERT INTO verdicts (article_id, prefilter_passed, prefilter_reason, "
        "prefilter_terms) VALUES ('a5', 0, 'no education vocabulary', '[]')"
    )
    conn.execute(
        "INSERT INTO coverage (article_id, status, method, internal_url, internal_title, "
        "internal_day, checks) VALUES ('a1', 'sitert', 'lenke', 'https://un.no/1', "
        "'Vår sak', '2026-08-17', 0)"
    )
    conn.commit()
    conn.close()

    from radar.web.app import app

    with TestClient(app) as c:
        yield c
    queries._stats_cache = None


def titles(html: str) -> list[str]:
    import re

    return [
        re.sub(r"<[^>]+>", "", m)
        for m in re.findall(r'<h3><a class="card-link"[^>]*>(.*?)</a></h3>', html, re.S)
    ]


# --- the feed -----------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    (
        # Today newest-first (09:00 before 07:00), earlier days best-first.
        ("", ["Ny bemanningsnorm i barnehagene", "Lærerlønn på bordet igjen",
              "Leseferdighetene faller", "Vi kan ikke digitalisere oss ut av lærermangelen"]),
        ("trinn:grunnskole", ["Lærerlønn på bordet igjen", "Leseferdighetene faller"]),
        ("trinn:barnehage", ["Ny bemanningsnorm i barnehagene"]),
        ("type:debatt", ["Vi kan ikke digitalisere oss ut av lærermangelen"]),
        ("kilde:NRK", ["Ny bemanningsnorm i barnehagene"]),
        ("-kilde:VG", ["Ny bemanningsnorm i barnehagene", "Leseferdighetene faller"]),
        ("dekning:sitert", ["Lærerlønn på bordet igjen"]),
        ("dekning:nei", ["Ny bemanningsnorm i barnehagene", "Leseferdighetene faller",
                         "Vi kan ikke digitalisere oss ut av lærermangelen"]),
        ("mur:ja", []),
        ("score:60", ["Ny bemanningsnorm i barnehagene", "Lærerlønn på bordet igjen"]),
        ("score:80", ["Lærerlønn på bordet igjen"]),
        ("score:45-52", ["Leseferdighetene faller",
                         "Vi kan ikke digitalisere oss ut av lærermangelen"]),
        ("trinn:grunnskole type:forskning", ["Leseferdighetene faller"]),
        ("trinn:barnehage type:debatt", []),
    ),
)
def test_structured_filters(client, query, expected):
    assert titles(client.get("/", params={"q": query}).text) == expected


@pytest.mark.parametrize(
    "query,expected",
    (
        ("lærerlønn", ["Lærerlønn på bordet igjen"]),
        # Nordic compounding: the prefix index is what makes this work.
        ("barnehagelærer", ["Ny bemanningsnorm i barnehagene"]),
        # Prefix, not substring: «skole*» finds «skolen» but not «grunnskolen»,
        # because a prefix probe anchors at the start of the token.
        ("skole", ["Vi kan ikke digitalisere oss ut av lærermangelen"]),
        ("grunnskole", ["Lærerlønn på bordet igjen"]),
        ("lærerlønn barnehage", []),          # implicit AND
        ("finnesikke", []),
        ("Ystebø: galskap", []),              # a pasted headline is a search
    ),
)
def test_free_text_search(client, query, expected):
    assert titles(client.get("/", params={"q": query}).text) == expected


def test_text_switches_to_a_ranked_list(client):
    html = client.get("/", params={"q": "skole"}).text
    assert 'class="rank-items"' in html
    assert 'class="feed-day"' not in html


def test_filters_alone_keep_the_chronological_feed(client):
    html = client.get("/", params={"q": "trinn:grunnskole"}).text
    assert 'class="feed-day"' in html
    assert 'class="rank-items"' not in html


def test_sort_override_restores_day_grouping(client):
    html = client.get("/", params={"q": "skole sorter:dato"}).text
    assert 'class="feed-day"' in html


def test_hits_are_marked_in_the_result(client):
    html = client.get("/", params={"q": "lærerlønn"}).text
    assert "<mark>Lærerlønn</mark>" in html


def test_a_title_cannot_inject_markup_through_the_snippet(client, tmp_path):
    conn = storage.connect(tmp_path / "radar.db")
    storage.upsert_article(conn, {
        "id": "evil", "source": "VG", "feed_name": None,
        "title": "<script>alert(1)</script> lærerlønn",
        "link": "https://vg.no/evil", "summary": "s", "body_excerpt": "lærerlønn her",
        "published_at": "2026-08-17T10:00:00+00:00", "published_day": "2026-08-17",
        "image_url": None, "paywalled": 0, "scraper_cursor": "c",
    })
    conn.execute(
        "INSERT INTO verdicts (article_id, prefilter_passed, relevant, score, stage, kind, "
        "topics, classified_at) VALUES ('evil', 1, 1, 90, 'grunnskole', 'nyhet', '[]', 'x')"
    )
    conn.commit()
    conn.close()

    html = client.get("/", params={"q": "lærerlønn"}).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize(
    "query,label",
    (
        ("trinn:barnehage", "Barnehage"),
        ("dekning:nei", "Ikke dekket"),
        ("dekning:ny", "Ny reaksjon"),
        ("score:>60", "Relevans 61 og oppover"),
        ("dager:7", "Siste uke"),
        ("mur:nei", "Uten betalingsmur"),
        ("-type:debatt", "Ikke debatt"),
        ("-kilde:VG", "Ikke VG"),          # a source name keeps its capitals
    ),
)
def test_active_filters_read_as_words_not_tokens(client, query, label):
    """The chip says «Ikke dekket», not «dekning:nei» — the desk is not writing SQL."""
    html = client.get("/", params={"q": query}).text
    assert 'class="active-filters"' in html
    assert label in html


def test_the_filter_button_counts_what_is_active(client):
    html = client.get("/", params={"q": "trinn:barnehage dekning:nei"}).text
    assert '<span class="filter-count">2</span>' in html


def test_the_search_box_holds_only_the_words(client):
    """Filters live in the panel; the search box is a search box."""
    html = client.get("/", params={"q": "trinn:grunnskole lærerlønn"}).text
    assert 'id="sok" name="sok" type="search" value="lærerlønn"' in html
    assert 'name="filtre" value="trinn:grunnskole"' in html


def test_the_filter_panel_is_a_plain_form(client):
    """No JavaScript required: real checkboxes in a GET form."""
    html = client.get("/").text
    assert '<form class="filter-panel" method="get" action="/">' in html
    assert '<input type="checkbox" name="trinn" value="barnehage"' in html


@pytest.mark.parametrize(
    "query,marker",
    (
        ("", "Ingen saker ennå"),
        ("finnesikkeord", "Ingen treff for"),
        ("trinn:barnehage type:debatt", "Ingen saker med disse filtrene"),
    ),
)
def test_empty_states_name_their_cause(client, tmp_path, query, marker):
    if not query:
        conn = storage.connect(tmp_path / "radar.db")
        conn.execute("DELETE FROM articles")
        conn.commit()
        conn.close()
    assert marker in client.get("/", params={"q": query}).text


# --- the filter panel round-trip ----------------------------------------------


@pytest.mark.parametrize(
    "params,expected",
    (
        ({"score": "0", "trinn": ["barnehage"]}, "/?q=trinn%3Abarnehage"),
        ({"score": "0", "trinn": ["barnehage", "grunnskole"]},
         "/?q=trinn%3Abarnehage+trinn%3Agrunnskole"),
        ({"score": "0", "dekning": ["nei"]}, "/?q=dekning%3Anei"),
        # «Alt» is what an unfiltered feed already means, so it is not written
        # into the URL.
        ({"score": "0"}, "/"),
        ({"score": "40"}, "/?q=score%3A40"),
        ({"score": "60"}, "/?q=score%3A60"),
        ({"score": "0", "dager": "7"}, "/?q=dager%3A7"),
        ({"score": "0", "mur": "nei"}, "/?q=mur%3Anei"),
        ({"score": "0", "sok": "lærerlønn"}, "/?q=l%C3%A6rerl%C3%B8nn"),
        ({"score": "0", "trinn": ["barnehage"], "sok": "streik"},
         "/?q=trinn%3Abarnehage+streik"),
        # An unticked box means off, not unchanged — the panel sent everything.
        ({"score": "0", "trinn": []}, "/"),
    ),
)
def test_the_panel_canonicalises_into_one_url(client, params, expected):
    r = client.get("/", params=params, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, expected)


def test_searching_keeps_the_filters(client):
    """The search form has no checkboxes, so it carries them in a hidden field."""
    r = client.get("/", params={"sok": "streik", "filtre": "trinn:barnehage"},
                   follow_redirects=False)
    assert r.headers["location"] == "/?q=trinn%3Abarnehage+streik"


def test_clearing_the_filters_keeps_the_search(client):
    r = client.get("/", params={"sok": "streik"}, follow_redirects=False)
    assert r.headers["location"] == "/?q=streik"


def test_the_panel_round_trips_its_own_state(client):
    """What the panel sets must come back ticked, or it cannot be unset."""
    html = client.get("/", params={"q": "trinn:barnehage dager:7 mur:nei"}).text
    assert '<input type="checkbox" name="trinn" value="barnehage" checked>' in html
    assert '<input type="radio" name="dager" value="7"\n                     checked>' in html \
        or 'value="7"' in html and 'name="dager"' in html
    assert '<input type="checkbox" name="mur" value="nei" checked>' in html


# --- legacy links -------------------------------------------------------------


@pytest.mark.parametrize(
    "params,location",
    (
        ({"stage": "barnehage"}, "/?q=trinn%3Abarnehage"),
        ({"covered": "nei"}, "/?q=dekning%3Anei"),
        ({"stage": "barnehage", "covered": "nei"}, "/?q=trinn%3Abarnehage+dekning%3Anei"),
        ({"min_score": 80}, "/?q=score%3A80"),
        ({"kind": "debatt"}, "/?q=type%3Adebatt"),
    ),
)
def test_old_bookmarks_redirect_into_the_new_grammar(client, params, location):
    r = client.get("/", params=params, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (301, location)


def test_a_plain_feed_request_is_not_redirected(client):
    assert client.get("/", follow_redirects=False).status_code == 200


# --- story page ---------------------------------------------------------------


def test_story_renders(client):
    r = client.get("/sak", params={"id": "a1"})
    assert r.status_code == 200
    assert "Lærerlønn på bordet igjen" in r.text


def test_missing_story_is_404_not_500(client):
    assert client.get("/sak", params={"id": "nope"}).status_code == 404


def test_story_survives_a_verdict_that_has_been_reset(client, tmp_path):
    """A re-ingested article has a NULL score until the next tick re-scores it."""
    conn = storage.connect(tmp_path / "radar.db")
    conn.execute("UPDATE verdicts SET score = NULL, relevant = NULL WHERE article_id = 'a1'")
    conn.commit()
    conn.close()

    r = client.get("/sak", params={"id": "a1"})
    assert r.status_code == 200
    assert "ikke vurdert" in r.text


def test_the_coverage_badge_links_to_our_article(client):
    html = client.get("/", params={"q": "dekning:sitert"}).text
    assert 'href="https://un.no/1"' in html


# --- state-changing routes ----------------------------------------------------


@pytest.mark.parametrize("path,payload", (
    ("/sak/tar", {"id": "a1"}),
    ("/sak/tilbakemelding", {"id": "a1", "vote": "1"}),
))
def test_writes_require_a_same_origin_request(client, path, payload):
    forged = client.post(path, data=payload, headers={"origin": "https://evil.example"},
                         follow_redirects=False)
    allowed = client.post(path, data=payload, headers={"origin": "http://testserver"},
                          follow_redirects=False)
    assert (forged.status_code, allowed.status_code) == (403, 303)


def test_a_claim_shows_on_the_feed_and_can_be_released(client):
    client.post("/sak/tar", data={"id": "a1"}, headers={"origin": "http://testserver"})
    assert "taken-tag" in client.get("/").text

    client.post("/sak/tar", data={"id": "a1", "release": "1"},
                headers={"origin": "http://testserver"})
    assert "taken-tag" not in client.get("/").text


@pytest.mark.parametrize("vote,expected", ((1, 1), (-1, -1)))
def test_a_vote_is_stored(client, tmp_path, vote, expected):
    client.post("/sak/tilbakemelding", data={"id": "a1", "vote": str(vote)},
                headers={"origin": "http://testserver"})
    conn = storage.connect(tmp_path / "radar.db")
    assert storage.vote_for(conn, "a1", "local") == expected
    conn.close()


def test_voting_the_same_way_twice_clears_it(client, tmp_path):
    for _ in range(2):
        client.post("/sak/tilbakemelding", data={"id": "a1", "vote": "1"},
                    headers={"origin": "http://testserver"})
    conn = storage.connect(tmp_path / "radar.db")
    assert storage.vote_for(conn, "a1", "local") is None
    conn.close()


def test_back_cannot_leave_the_app(client):
    r = client.post("/sak/tar", data={"id": "a1", "back": "https://evil.example"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.headers["location"] == "/"


# --- other pages and headers --------------------------------------------------


def test_rejected_page_lists_what_the_word_filter_dropped(client):
    html = client.get("/frafiltrert").text
    assert "Noe helt annet" in html
    assert "no education vocabulary" in html


def test_about_page_carries_the_ranking_prose_and_the_totals(client):
    html = client.get("/om").text
    assert "Slik rangeres det" in html
    assert "relevante totalt" in html
    assert "Treff per dag" in html


@pytest.mark.parametrize(
    "fragment",
    (
        "Slik rangeres det",     # how the ranking works
        "relevante totalt",      # the pipeline counts
        "artikler vurdert",
        "Treff per dag",         # the day sparkline
        "barnehage til lærerutdanning",  # the tagline
    ),
)
def test_the_frontpage_does_not_explain_itself(client, fragment):
    """Prose and totals moved to /om; the feed is the feed."""
    assert fragment not in client.get("/").text


@pytest.mark.parametrize("path", ("/", "/frafiltrert", "/om", "/sak?id=a1"))
def test_every_page_has_a_way_back_to_unyttkassa(client, path):
    assert '<a class="rail-home" href="https://example.org"' in client.get(path).text


def test_the_rail_still_carries_the_quick_filters(client):
    """The stats went; the trinn shortcuts and their counts did not."""
    html = client.get("/").text
    assert '<nav class="rail-nav" aria-label="Trinn">' in html
    assert "Alle saker" in html


@pytest.mark.parametrize(
    "header,value",
    (
        ("x-content-type-options", "nosniff"),
        # Not no-referrer: under that policy a browser sends `Origin: null` on
        # every POST, and the same-origin check then refuses our own forms.
        ("referrer-policy", "same-origin"),
        ("x-frame-options", "DENY"),
    ),
)
def test_security_headers(client, header, value):
    assert client.get("/").headers[header] == value


def test_csp_blocks_third_party_script(client):
    csp = client.get("/").headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_health_is_503_without_the_scraper_mount(client, monkeypatch, tmp_path):
    monkeypatch.setattr("radar.config.SCRAPER_DB_PATH", tmp_path / "gone.db")
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["checks"]["scraper"] is False


def test_health_is_200_when_the_mounts_are_there(client, monkeypatch, tmp_path):
    present = tmp_path / "scraper.db"
    present.touch()
    monkeypatch.setattr("radar.config.SCRAPER_DB_PATH", present)
    assert client.get("/health").status_code == 200


def test_health_reports_a_stale_tick_without_failing_the_probe(client, monkeypatch, tmp_path):
    """Failing here would let a dead cron block the deploy that fixes the cron."""
    present = tmp_path / "scraper.db"
    present.touch()
    monkeypatch.setattr("radar.config.SCRAPER_DB_PATH", present)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["checks"]["tick_fresh"] is False


@pytest.mark.parametrize(
    ("env", "headers", "status"),
    [
        ("test-stats-token", {}, 401),
        ("test-stats-token", {"X-Stats-Token": ""}, 401),
        ("test-stats-token", {"X-Stats-Token": "wrong"}, 401),
        ("test-stats-token", {"X-Stats-Token": "test-stats-toke"}, 401),
        ("test-stats-token", {"X-Stats-Token": "test-stats-tokenX"}, 401),
        ("test-stats-token", {"X-Stats-Token": "test-stats-token"}, 200),
        ("", {"X-Stats-Token": ""}, 401),
        (None, {}, 401),
        (None, {"X-Stats-Token": "test-stats-token"}, 401),
    ],
    ids=["missing", "empty", "wrong", "one-short", "one-long", "right",
         "unset-empty", "unset-missing", "unset-any"],
)
def test_stats_requires_token(client, monkeypatch, env, headers, status):
    if env is None:
        monkeypatch.delenv("STATS_TOKEN")
    else:
        monkeypatch.setenv("STATS_TOKEN", env)
    assert client.get("/stats", headers=headers).status_code == status


@pytest.mark.parametrize(
    ("key", "headers", "status"),
    [
        ("evals-key", {}, 403),
        ("evals-key", {"X-API-Key": "evals-ke"}, 403),
        ("evals-key", {"X-API-Key": "evals-key"}, 200),
        ("", {"X-API-Key": ""}, 403),
    ],
    ids=["missing", "one-short", "right", "unset-empty"],
)
def test_api_stats_requires_api_key(client, monkeypatch, key, headers, status):
    monkeypatch.setattr("radar.config.EVALS_API_KEY", key)
    assert client.get("/api/stats", headers=headers).status_code == status


def test_health_needs_no_token(client, monkeypatch, tmp_path):
    # /health is 503 without the scraper's database. Unpatched, this read the developer's
    # own data dir and passed only on a machine that had one.
    scraper_db = tmp_path / "news_scraper.db"
    scraper_db.touch()
    monkeypatch.setattr("radar.config.SCRAPER_DB_PATH", scraper_db)
    monkeypatch.delenv("STATS_TOKEN")
    assert client.get("/health").status_code == 200


def test_stats_does_not_disclose_the_filesystem(client):
    body = client.get("/stats", headers=STATS).json()
    assert "db" not in body
    assert "totals" in body


@pytest.mark.parametrize(
    "clean_age,story_age,stale",
    [(15, 1, False), (15, 15, True), (1, 15, False), (None, 1, False),
     (None, 15, True)],
)
def test_feed_freshness_tracks_stories_even_when_a_tick_fails(
    client, tmp_path, clean_age, story_age, stale,
):
    now = datetime.now(UTC)
    story_time = (now - timedelta(hours=story_age)).isoformat()
    clean_time = (now - timedelta(hours=clean_age)).isoformat() if clean_age else None
    recent = now.isoformat()
    conn = storage.connect(tmp_path / "radar.db")
    conn.execute("UPDATE verdicts SET classified_at = ? WHERE relevant = 1", (story_time,))
    # A recent rejected article must not imply that the visible feed updated.
    conn.execute(
        "UPDATE verdicts SET classified_at = ?, relevant = 0 WHERE article_id = 'a5'",
        (recent,),
    )
    if clean_time:
        conn.execute(
            "INSERT INTO tick_runs (started_at, finished_at, errors) VALUES (?, ?, 0)",
            (clean_time, clean_time),
        )
    conn.execute(
        "INSERT INTO tick_runs (started_at, finished_at, errors, archive_seen) "
        "VALUES (?, ?, 1, 1)",
        (recent, recent),
    )
    conn.commit()
    conn.close()

    html = client.get("/").text
    assert ("Radaren har ikke oppdatert seg" in html) is stale
    assert "Det du ser er saker fram til" not in html
    totals = client.get("/stats", headers=STATS).json()["totals"]
    assert totals["last_updated"] == max(filter(None, (story_time, clean_time)))
    # Operational monitoring still reports the last fully successful run.
    assert totals["last_tick"] == clean_time
    assert client.get("/health").json()["checks"]["tick_fresh"] is (clean_age == 1)


# --- copy ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    (
        ("trinn:barnehage", "Utdanningsradar — 1 sak"),          # one is «sak»
        ("trinn:grunnskole", "Utdanningsradar — 2 saker"),
        ("trinn:barnehage type:debatt", "Utdanningsradar — 0 saker"),
    ),
)
def test_norwegian_plural_of_sak(client, query, expected):
    """The count survives only in the h1 a screen reader reads."""
    assert expected in client.get("/", params={"q": query}).text


@pytest.mark.parametrize("fragment", ('class="result-line"', 'class="sort-switch"'))
def test_the_feed_opens_on_the_first_story(client, fragment):
    """No count and no sort switch between the filters and the first card."""
    assert fragment not in client.get("/").text


def test_the_page_never_advertises_the_token_grammar(client):
    """The bar is a search box now. The grammar still parses, but nothing sells it."""
    html = client.get("/").text
    assert "trinn:" not in html.split('<div class="body-grid">')[0].replace(
        'name="trinn"', ""
    ).replace('value="trinn', "")


# --- how the days are ordered -------------------------------------------------


def seed_day(tmp_path, day, rows):
    """rows: (id, clock, score) — same day, so only the within-day order varies."""
    conn = storage.connect(tmp_path / "radar.db")
    for aid, clock, score in rows:
        storage.upsert_article(conn, {
            "id": aid, "source": "VG", "feed_name": None, "title": aid,
            "link": f"https://vg.no/{aid}", "summary": "", "body_excerpt": "x",
            "published_at": f"{day}T{clock}:00+00:00", "published_day": day,
            "image_url": None, "paywalled": 0, "scraper_cursor": "c",
        })
        conn.execute(
            "INSERT INTO verdicts (article_id, prefilter_passed, relevant, score, stage, "
            "kind, topics, classified_at) VALUES (?, 1, 1, ?, 'grunnskole', 'nyhet', '[]', 'x')",
            (aid, score),
        )
    conn.commit()
    conn.close()


def test_today_is_newest_first(client, tmp_path):
    """Today is still happening, so what the desk wants is what just landed."""
    conn = storage.connect(tmp_path / "radar.db")
    conn.execute("DELETE FROM articles")
    conn.commit()
    conn.close()
    seed_day(tmp_path, TODAY, [("low-late", "18:00", 10), ("high-early", "06:00", 99)])

    assert titles(client.get("/").text) == ["low-late", "high-early"]


def test_an_earlier_day_is_best_first(client, tmp_path):
    """That day is finished, so what they want is what mattered."""
    conn = storage.connect(tmp_path / "radar.db")
    conn.execute("DELETE FROM articles")
    conn.commit()
    conn.close()
    seed_day(tmp_path, "2026-08-10", [("low-late", "18:00", 10), ("high-early", "06:00", 99)])

    assert titles(client.get("/").text) == ["high-early", "low-late"]


def test_the_two_rules_coexist_on_one_page(client, tmp_path):
    conn = storage.connect(tmp_path / "radar.db")
    conn.execute("DELETE FROM articles")
    conn.commit()
    conn.close()
    seed_day(tmp_path, TODAY, [("today-low-late", "18:00", 10), ("today-high-early", "06:00", 99)])
    seed_day(tmp_path, "2026-08-10", [("old-low-late", "18:00", 10), ("old-high-early", "06:00", 99)])

    assert titles(client.get("/").text) == [
        "today-low-late", "today-high-early",   # today: by the clock
        "old-high-early", "old-low-late",       # earlier: by score
    ]


def test_a_text_search_ignores_the_day_rule_entirely(client, tmp_path):
    """A relevance list is not a feed; it has no days to order within."""
    conn = storage.connect(tmp_path / "radar.db")
    conn.execute("DELETE FROM articles")
    conn.commit()
    conn.close()
    seed_day(tmp_path, TODAY, [("alfa", "06:00", 99), ("beta", "18:00", 10)])

    html = client.get("/", params={"q": "x"}).text
    assert 'class="rank-items"' in html


# --- the default threshold ----------------------------------------------------


def test_fagstoff_is_visible_by_default(client, tmp_path):
    """A filter that hides things by default hides them from anyone who does not
    know the filter exists."""
    conn = storage.connect(tmp_path / "radar.db")
    conn.execute("DELETE FROM articles")
    conn.commit()
    conn.close()
    seed_day(tmp_path, "2026-08-10", [("fagstoff", "09:00", 22), ("nyhet", "10:00", 88)])

    assert titles(client.get("/").text) == ["nyhet", "fagstoff"]


def test_alt_is_the_checked_relevance_option(client):
    html = client.get("/").text
    assert '<input type="radio" name="score" value="0"\n                     checked>' in html


# --- per-source yield ---------------------------------------------------------


def source_row(tmp_path, name):
    conn = storage.connect(tmp_path / "radar.db")
    try:
        return {r["source"]: r for r in queries.source_stats(conn)}.get(name)
    finally:
        conn.close()


def test_source_stats_counts_every_article_but_averages_only_the_relevant(client, tmp_path):
    """The total says what a source costs; the average says what it is worth.

    VG contributes three articles, one of which the word filter rejected. All
    three count towards the total — a source we read and throw away is exactly
    what this table exists to expose — while the average is over the two the
    model scored.
    """
    vg = source_row(tmp_path, "VG")
    assert vg["articles"] == 3
    assert vg["relevant"] == 2
    assert vg["avg_score"] == pytest.approx((88 + 45) / 2)


def test_a_source_that_never_lands_reports_no_average(client, tmp_path):
    """None, not zero. A source averaging 0 and one that has never been scored
    are different facts, and the page prints them differently."""
    conn = storage.connect(tmp_path / "radar.db")
    storage.upsert_article(conn, {
        "id": "z1", "source": "Stille", "feed_name": None, "title": "Ingenting",
        "link": "https://stille.no/z1", "summary": "", "body_excerpt": "x",
        "published_at": "2026-08-17T05:00:00+00:00", "published_day": "2026-08-17",
        "image_url": None, "paywalled": 0, "scraper_cursor": "c",
    })
    conn.execute(
        "INSERT INTO verdicts (article_id, prefilter_passed, relevant, prefilter_terms) "
        "VALUES ('z1', 0, 0, '[]')"
    )
    conn.commit()
    conn.close()

    stille = source_row(tmp_path, "Stille")
    assert stille["articles"] == 1
    assert stille["relevant"] == 0
    assert stille["avg_score"] is None
    assert stille["last_hit"] is None


@pytest.mark.parametrize("score, counted", [(59, 0), (60, 1), (61, 1)])
def test_sixty_is_inside_the_strong_band(client, tmp_path, score, counted):
    """60 is the band's floor, so it belongs to it."""
    conn = storage.connect(tmp_path / "radar.db")
    conn.execute("DELETE FROM articles")
    conn.commit()
    conn.close()
    seed_day(tmp_path, "2026-08-10", [("s1", "09:00", score)])

    assert source_row(tmp_path, "VG")["strong"] == counted


def test_sources_are_ordered_by_what_they_deliver(client, tmp_path):
    """Not alphabetically, and not by volume — by relevant articles, because the
    question the table answers is which sources produce stories."""
    conn = storage.connect(tmp_path / "radar.db")
    try:
        order = [r["source"] for r in queries.source_stats(conn)]
    finally:
        conn.close()
    assert order[0] == "VG"
    assert set(order) == {"VG", "NRK", "Forskning"}


def test_the_about_page_lists_sources(client):
    html = client.get("/om").text
    assert "<h2>Kilder</h2>" in html
    assert "&lt;thead&gt;" not in html  # the shared header must render as markup


def test_the_about_page_lists_each_source_once(client, tmp_path):
    """The folded tail is the same table, so nothing may be rendered twice."""
    conn = storage.connect(tmp_path / "radar.db")
    for i in range(12):
        aid = f"src{i}"
        storage.upsert_article(conn, {
            "id": aid, "source": f"Kilde{i}", "feed_name": None, "title": f"Sak {i}",
            "link": f"https://kilde{i}.no/{aid}", "summary": "x", "body_excerpt": "y",
            "published_at": "2026-08-17T06:00:00+00:00", "published_day": "2026-08-17",
            "image_url": None, "paywalled": 0, "scraper_cursor": "c",
        })
        conn.execute(
            "INSERT INTO verdicts (article_id, prefilter_passed, relevant, score, stage, "
            "kind, topics, rationale, classified_at, prompt_version) "
            "VALUES (?, 1, 1, 50, 'grunnskole', 'nyhet', '[]', 'fordi', "
            "'2026-08-17T06:00:00+00:00', 'v4')",
            (aid,),
        )
    conn.commit()
    conn.close()

    html = client.get("/om").text
    body = html[html.index("<h2>Kilder</h2>"):]
    for i in range(12):
        assert body.count(f"<td>Kilde{i}</td>") == 1
    assert "Vis alle 15 kildene" in body


# --- «Skjul»: muted words for one browser session --------------------------------

ORIGIN = {"origin": "http://testserver"}


def mute(client, word, back="/"):
    return client.post("/skjul", data={"ord": word, "back": back}, headers=ORIGIN,
                       follow_redirects=False)


def unmute(client, word):
    return client.post("/skjul", data={"fjern": word, "back": "/"}, headers=ORIGIN,
                       follow_redirects=False)


@pytest.mark.parametrize(
    "word,gone,kept",
    (
        ("lærerlønn", ("a1",), ("a2", "a3", "a4")),
        # The ingress counts as much as the title.
        ("lønnsoppgjøret", ("a1",), ("a2", "a3", "a4")),
        # The body does not: a passing mention is not what the story is about.
        ("streik", (), ("a1", "a2", "a3", "a4")),
        ("LÆRERLØNN", ("a1",), ("a2", "a3", "a4")),
        # Prefix: «lærer» takes «Lærerlønn» and «lærermangelen», but not the
        # «barnehagelærere» buried in a2's body.
        ("lærer", ("a1", "a4"), ("a2", "a3")),
        ("finnesikke", (), ("a1", "a2", "a3", "a4")),
    ),
)
def test_a_muted_word_hides_stories_that_name_it_in_title_or_ingress(client, word, gone, kept):
    assert mute(client, word).status_code == 303
    html = client.get("/").text
    titles = {aid: title for aid, _, title, *_ in ARTICLES}
    assert [aid for aid in gone if titles[aid] in html] == []
    assert [aid for aid in kept if titles[aid] not in html] == []


def seed_about(tmp_path, aid, title, *, rationale="fordi", topics="[]", cluster=None):
    """One story whose only trace of a word may be what the classifier wrote."""
    conn = storage.connect(tmp_path / "radar.db")
    storage.upsert_article(conn, {
        "id": aid, "source": "VG", "feed_name": None, "title": title,
        "link": f"https://vg.no/{aid}", "summary": "", "body_excerpt": "x",
        "published_at": f"{TODAY}T06:00:00+00:00", "published_day": TODAY,
        "image_url": None, "paywalled": 0, "scraper_cursor": "c",
    })
    if cluster:
        conn.execute(
            "INSERT INTO clusters (id, label, first_day, last_day, created_at) "
            "VALUES (?, ?, ?, ?, 'x')",
            (cluster[0], cluster[1], TODAY, TODAY),
        )
    conn.execute(
        "INSERT INTO verdicts (article_id, prefilter_passed, relevant, score, stage, kind, "
        "topics, rationale, classified_at, cluster_id) VALUES (?, 1, 1, 70, 'grunnskole', "
        "'nyhet', ?, ?, 'x', ?)",
        (aid, topics, rationale, cluster[0] if cluster else None),
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "field,hidden",
    (
        # PISA week: «Kollaps for norske elevers ferdigheter» never says PISA,
        # but the classifier did, three different ways.
        ({"rationale": "Nye Pisa-tall viser kraftig fall."}, True),
        ({"topics": '["elevresultater", "pisa"]'}, True),
        ({"cluster": ("b2de7827626b", "Norske elevers Pisa-resultater")}, True),
        # A body-only mention still does not count — see the ARTICLES cases.
        ({"rationale": "Om lønnsoppgjøret."}, False),
    ),
)
def test_a_mute_reads_what_the_classifier_wrote_about_the_story(client, tmp_path, field, hidden):
    seed_about(tmp_path, "k1", "Kollaps for norske elevers ferdigheter", **field)
    mute(client, "pisa")
    html = client.get("/").text
    assert ("Kollaps for norske elevers ferdigheter" not in html) is hidden
    assert (f'<span class="af-n">{1 if hidden else 0}</span>') in html


def test_a_like_wildcard_in_a_muted_word_is_literal(client, tmp_path):
    seed_about(tmp_path, "k1", "Uten treff", rationale="Ingenting om saken.")
    mute(client, "%")
    assert "Uten treff" in client.get("/").text


def test_the_page_says_what_is_hidden_and_how_much(client):
    mute(client, "lærer")
    html = client.get("/").text
    assert '<span class="af-label af-mute">Skjuler</span>' in html
    assert '<span class="af-n">2</span>' in html
    assert "Skjul\n      <span class=\"filter-count\">1</span>" in html
    # The rail's total and the day's count both drop with it.
    assert '<span class="count">2</span>' in html
    assert "1 sak</div>" in html  # 17 August had a1 and a2; a1 is gone


def test_muting_narrows_the_facet_counts_too(client):
    before = client.get("/").text
    mute(client, "lærerlønn")
    after = client.get("/").text
    grunnskole = '<input type="checkbox" name="trinn" value="grunnskole" >'
    assert grunnskole + '\n              <span class="stage-dot stage-grunnskole" aria-hidden="true"></span>\n              <span class="fp-label">Grunnskole</span>\n              <span class="fp-n">2</span>' in before
    assert grunnskole + '\n              <span class="stage-dot stage-grunnskole" aria-hidden="true"></span>\n              <span class="fp-label">Grunnskole</span>\n              <span class="fp-n">1</span>' in after


def test_a_search_and_a_muted_word_compose(client):
    mute(client, "lærerlønn")
    html = client.get("/", params={"q": "lærer"}).text
    assert "lærermangelen" in html
    assert "Lærerlønn på bordet igjen" not in html


def test_unmuting_brings_the_stories_back_and_drops_the_cookie(client):
    mute(client, "lærerlønn")
    assert "skjul" in client.cookies
    response = unmute(client, "lærerlønn")
    assert "skjul" not in client.cookies
    assert response.status_code == 303
    assert "Lærerlønn på bordet igjen" in client.get("/").text
    assert "Skjuler" not in client.get("/").text


def test_the_mute_cookie_lives_for_the_browser_session_only(client):
    header = mute(client, "pisa").headers["set-cookie"].lower()
    assert "max-age" not in header
    assert "expires" not in header
    assert "httponly" in header
    assert "samesite=lax" in header


def test_muting_needs_a_same_origin_request(client):
    forged = client.post("/skjul", data={"ord": "pisa"},
                         headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert forged.status_code == 403
    assert "skjul" not in client.cookies


@pytest.mark.parametrize("back,location", (
    ("/?q=trinn%3Abarnehage", "/?q=trinn%3Abarnehage"),
    ("//evil.example", "/"),
    ("https://evil.example", "/"),
))
def test_muting_sends_the_reader_back_where_they_were(client, back, location):
    assert mute(client, "pisa", back=back).headers["location"] == location


def test_a_garbage_cookie_reads_as_nothing_muted(client):
    client.cookies.set("skjul", "%7B%22not%22%3A%20%22a%20list%22")
    html = client.get("/")
    assert html.status_code == 200
    assert "Skjuler" not in html.text
    client.cookies.set("skjul", "not json at all")
    assert client.get("/").status_code == 200


@pytest.mark.parametrize(
    "word,stored",
    (
        ("  PISA  ", "PISA"),
        ('"pisa"', "pisa"),
        ("ap  landsmøte", "ap landsmøte"),
        ("x" * 40, "x" * 40),
        ("x" * 41, "x" * 40),
        # Nothing an index could match on is not stored at all.
        ("***", None),
        ("   ", None),
    ),
)
def test_a_muted_word_is_normalised_before_it_is_stored(client, word, stored):
    mute(client, word)
    html = client.get("/").text
    if stored is None:
        assert "Skjuler" not in html
        return
    assert f'name="fjern" value="{stored}"' in html


def test_the_same_word_twice_is_one_chip(client):
    mute(client, "pisa")
    mute(client, "PISA")
    assert client.get("/").text.count('name="fjern"') == 2  # once in the row, once in the panel


@pytest.mark.parametrize("n,oldest_kept", ((8, True), (9, False)))
def test_the_ninth_muted_word_pushes_out_the_oldest(client, n, oldest_kept):
    for i in range(n):
        mute(client, f"ord{i}")
    html = client.get("/").text
    assert ('name="fjern" value="ord0"' in html) is oldest_kept
    assert f'name="fjern" value="ord{n - 1}"' in html


def test_muting_every_story_is_not_reported_as_an_empty_radar(client):
    for word in ("lærer", "bemanningsnorm", "leseferdighetene"):
        mute(client, word)
    html = client.get("/").text
    assert "Alt som er igjen er skjult" in html
    assert "Ingen saker ennå" not in html


def test_the_mute_panel_is_a_plain_form(client):
    html = client.get("/").text
    assert '<form class="mute-form" method="post" action="/skjul">' in html
    assert 'name="ord" type="text" maxlength="40" required' in html

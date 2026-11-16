"""Kunngjøringer read from w2.brreg.no's public search (saved pages in fixtures/kunngjoringer).

The pages are as Brreg served them, except the people: their names and addresses are made up. In the
bankruptcy notice the company, its address, the case number and the trustee are made up as well."""

import asyncio
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from src import db, kunngjoringer
from src.kunngjoringer import parse_list, parse_one

FIX = Path(__file__).parent / "fixtures" / "kunngjoringer"
LIQUID, EQUINOR = "813816032", "923609016"


def page(name: str) -> str:
    return (FIX / f"{name}.html").read_text(encoding="latin-1")


def test_list_is_every_announcement_newest_first():
    items = parse_list(page(f"list_{LIQUID}"))
    assert (len(items), items[0], items[-1]) == (
        52,
        {"kid": "20260000775048", "date": "2026-10-05", "type": "Revisor"},
        {
            "kid": "20140000246212",
            "date": "2014-06-26",
            "type": "Nyregistrering i Foretaksregisteret",
        },
    )


def test_list_row_without_a_date_takes_the_one_above():
    """Equinor 02.07.2026: «Kapital» with the date, «Vedtektsdato» on the next row without."""
    items = parse_list(page(f"list_{EQUINOR}"))
    i = next(i for i, it in enumerate(items) if it["kid"] == "20260000484190")
    assert (len(items), items[i - 1 : i + 1]) == (
        195,
        [
            {"kid": "20260000484191", "date": "2026-07-02", "type": "Kapital"},
            {"kid": "20260000484190", "date": "2026-07-02", "type": "Vedtektsdato"},
        ],
    )


def test_unregistered_orgnr_has_no_list():
    assert (
        parse_list("<p>Du søkte på: 999 999 999 Enheten er ikke registrert i Enhetsregisteret.</p>")
        is None
    )


@pytest.mark.parametrize(
    "kid,expected",
    [
        (
            "20260000775048",
            {
                "title": "Endring av revisor",
                "register": "Foretaksregisteret",
                "date": "2026-10-05",
                "name": "LIQUID BARCODES AS",
                "address": "Lensmannslia 4, 1386 ASKER",
                "parts": [
                    [
                        "f",
                        "Revisor",
                        "Org.nr 935 416 779 CEDRA NORGE AS, Ruseløkkveien 30, 0251 OSLO",
                    ]
                ],
            },
        ),
        (
            "20240000663510",
            {
                "title": "Endring av styre",
                "register": "Foretaksregisteret",
                "date": "2024-07-05",
                "name": "LIQUID BARCODES AS",
                "address": "Elias Smiths vei 24, 1337 SANDVIKA",
                "parts": [
                    ["f", "Styre", ""],
                    ["f", "Styrets leder", "Lars Holm, Storgata 1, 7010 TRONDHEIM"],
                    [
                        "f",
                        "Styremedlem",
                        "Peder Ås\nMarte Kirkerud\nNils Nilsen\n"
                        "Kari Nordmann\nRepresentant for A-aksjonærene\nOla Nordmann\n"
                        "Representant for A-aksjonærene",
                    ],
                ],
            },
        ),
        (
            "20260000607242",
            {
                "title": "Godkjent årsregnskap",
                "register": "Regnskapsregisteret",
                "date": "2026-07-27",
                "name": "LIQUID BARCODES AS",
                "address": "Lensmannslia 4, 1386 ASKER",
                "parts": [
                    [
                        "p",
                        "Regnskapsregisteret har den 26.06.2026 mottatt komplett årsregnskap for regnskapsåret 2025 fra:",
                    ],
                    [
                        "p",
                        "Årsregnskapet omfatter perioden fra 01.01.2025 til 31.12.2025. Foretaket er i denne perioden registrert som morselskap i konsern.",
                    ],
                ],
            },
        ),
        (
            "20250000422779",
            {
                "title": "Endring av forretningsadresse",
                "register": "Foretaksregisteret",
                "date": "2025-06-17",
                "name": "LIQUID BARCODES AS",
                "address": "Elias Smiths vei 24, 1337 SANDVIKA",
                "parts": [
                    ["f", "Ny forretningsadresse", "Lensmannslia 4\n1386 ASKER"],
                    ["f", "Kommune", "3203 ASKER"],
                ],
            },
        ),
    ],
    ids=["revisor", "styre-several-members", "arsregnskap-prose", "address-keeps-new-kommune"],
)
def test_announcement_parts(kid, expected):
    d = parse_one(page(f"one_{kid}"))
    assert {k: d[k] for k in expected} == expected


def test_formal_is_running_text_not_lines():
    d = parse_one(page("one_20150000003360"))
    assert d["parts"] == [
        ["f", "Formål", "Utvikling, drift og salg av markedsføringsløsninger og billetteringssystemer "
         "med dertil tilhørende teknologi, handel og tjenesteyting og alt der omkringliggende i inn- og "
         "utland, samt deltagelse i annen virksomhet."]
    ]  # fmt: skip


def test_merger_keeps_every_company_under_its_role():
    """No <h3>: the title is the first bold line, and each company's details stay under its role."""
    d = parse_one(page("one_20070000144686"))
    heads = [p[1] for p in d["parts"] if p[0] == "h"]
    names = [p[2] for p in d["parts"] if p[0] == "f" and p[1] == "Navn/foretaksnavn"]
    assert (d["title"], d["name"], heads, names) == (
        "Fusjonsplan",
        None,
        [
            "Foretaksregisteret har mottatt fusjonsplan for:",
            "Overtakende selskap",
            "Overdragende selskap",
        ],
        ["STATOIL ASA", "Norsk Hydro ASA"],
    )


def test_bankruptcy_bold_sentence_is_one_heading():
    d = parse_one(page("one_20260000000001"))
    assert (d["title"], d["register"], d["parts"][0], d["parts"][5]) == (
        "Konkurs - åpning",
        "Konkursregisteret",
        ["h", "Ved HORDALAND TINGRETT er det åpnet konkurs i boet til:"],
        ["f", "Konkurs åpnet", "18.09.2026"],
    )


def test_text_is_the_whole_notice_for_search():
    assert (
        parse_one(page("one_20230000696926"))["text"]
        == "Endring av kapital\nKapital: NOK 159.688,54"
    )


# ---------- endpoint ----------


class FakeW2:
    """w2.brreg.no serving the saved pages; `down` makes it fail like an outage."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.down = False
        self.list_page = page(f"list_{LIQUID}")

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.hits.append(request.url.path)
        if self.down:
            return httpx.Response(503)
        if request.url.path.endswith("hent_nr.jsp"):
            if request.url.params["orgnr"] != LIQUID:
                return httpx.Response(200, text="Enheten er ikke registrert i Enhetsregisteret.")
            return httpx.Response(200, text=self.list_page)
        kid = request.url.params["kid"]
        name = f"one_{kid}" if (FIX / f"one_{kid}.html").exists() else "one_20230000696926"
        return httpx.Response(200, text=page(name))


@pytest.fixture
def w2(app_client, brreg):
    fake = FakeW2()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="w2.brreg.no").mock(side_effect=fake.handle)
        router.route(host="data.brreg.no").mock(side_effect=brreg.handle)
        yield fake


URL = f"/api/company/{LIQUID}/kunngjoringer"
NEWEST = "20260000775048"


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(kunngjoringer, "PACE_GAP", 0.0)


def test_first_view_reads_the_list_and_the_newest_batch(app_client, w2):
    body = app_client.get(URL).json()
    first, read = body["items"][0], [it["read"] for it in body["items"]]
    assert (len(w2.hits), len(body["items"]), body["unread"], read[11:13], body["stale"]) == (
        13,
        52,
        40,
        [True, False],
        False,
    )
    assert (first["kid"], first["title"], first["date"], first["url"]) == (
        NEWEST,
        "Endring av revisor",
        "2026-10-05",
        f"https://w2.brreg.no/kunngjoring/hent_en.jsp?kid={NEWEST}&sokeverdi={LIQUID}&spraak=nb",
    )


def test_each_later_view_reads_the_next_batch_but_not_the_list(app_client, w2):
    app_client.get(URL)
    w2.hits.clear()
    body = app_client.get(URL).json()
    assert ([h.rsplit("/", 1)[1] for h in w2.hits], body["unread"]) == (["hent_en.jsp"] * 12, 28)


def test_once_all_are_read_a_view_within_a_day_asks_brreg_nothing(app_client, w2):
    for _ in range(5):
        app_client.get(URL)
    w2.hits.clear()
    assert (app_client.get(URL).json()["unread"], w2.hits) == (0, [])


def test_next_day_reads_the_list_and_only_new_announcements(app_client, w2):
    for _ in range(5):
        app_client.get(URL)
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    db._run("UPDATE kunngjoring_lists SET fetched_at = ?", (old,))
    new_row = '<tr><td>06.10.2026</td><td><a href="hent_en.jsp?kid=20260000999999&amp;sokeverdi=1">Styre</a></td></tr>'
    w2.list_page = re.sub(
        r"(<tr[^>]*>(?:(?!</tr>).)*hent_en\.jsp)",
        new_row + r"\1",
        w2.list_page,
        count=1,
        flags=re.S,
    )
    w2.hits.clear()
    items = app_client.get(URL).json()["items"]
    assert ([h.rsplit("/", 1)[1] for h in w2.hits], items[0]["kid"], len(items)) == (
        ["hent_nr.jsp", "hent_en.jsp"],
        "20260000999999",
        53,
    )


def test_an_unread_announcement_is_read_when_opened(app_client, w2):
    app_client.get(URL)
    oldest = "20140000246212"
    w2.hits.clear()
    one = app_client.get(f"{URL}/{oldest}").json()
    again = app_client.get(f"{URL}/{oldest}").json()
    assert (one["title"], one["read"], len(w2.hits), again == one) == (
        "Nyregistrering",
        True,
        1,
        True,
    )


@pytest.mark.parametrize(
    "url",
    [f"{URL}/99999999999999", f"/api/company/{EQUINOR}/kunngjoringer/{NEWEST}"],
    ids=["unknown", "other-company"],
)
def test_opening_an_announcement_not_listed_for_the_company_is_404(app_client, w2, url):
    app_client.get(URL)
    assert app_client.get(url).status_code == 404


def test_outage_serves_what_we_have(app_client, w2):
    app_client.get(URL)
    db._run("UPDATE kunngjoring_lists SET fetched_at = '2000-01-01T00:00:00+00:00'")
    w2.down = True
    body = app_client.get(URL).json()
    assert (len(body["items"]), body["unread"], body["stale"]) == (52, 40, True)


def test_outage_without_anything_stored_is_503(app_client, w2):
    w2.down = True
    assert app_client.get(URL).status_code == 503


def test_unregistered_orgnr_is_404(app_client, w2):
    resp = app_client.get(f"/api/company/{EQUINOR}/kunngjoringer")
    assert (resp.status_code, resp.json()["detail"]) == (
        404,
        "Brreg har ingen kunngjøringer for dette org.nr.",
    )


class SlowW2:
    """Records how many requests are in flight and when each starts."""

    def __init__(self) -> None:
        self.live = self.most = 0
        self.starts: list[float] = []

    async def get_html(self, url: str) -> str:
        self.starts.append(time.monotonic())
        self.live += 1
        self.most = max(self.most, self.live)
        await asyncio.sleep(0.02)
        self.live -= 1
        return page("one_20230000696926")


async def test_pacer_keeps_two_in_flight_and_starts_apart(monkeypatch):
    monkeypatch.setattr(kunngjoringer, "PACE_GAP", 0.01)
    w2 = SlowW2()
    got = await kunngjoringer.fetch_details(w2, LIQUID, [str(i) for i in range(8)])
    gaps = [b - a for a, b in zip(w2.starts, w2.starts[1:], strict=False)]
    assert (len(got), w2.most, min(gaps) >= 0.009) == (8, 2, True)


@pytest.mark.parametrize(
    "kid,title,parts",
    [
        (
            "20060000220773",
            "Godkjent årsregnskap",
            [
                [
                    "p",
                    "Regnskapsregisteret har den 12.06.2006 mottatt og godkjent årsregnskapet for regnskapsåret 2005 fra:",
                ],
                [
                    "p",
                    "Årsregnskapet omfatter perioden fra 01.01.2005 til 31.12.2005. Foretaket er i denne perioden registrert som morselskap i konsern.",
                ],
            ],
        ),
        (
            "20060000266056",
            "Nedsettelse av overkursfond",
            [
                [
                    "p",
                    "Beslutning av 09.05.2006 vedrørende nedsettelse av overkursfond med NOK 821.215.891,50 er registrert gjennomført.",
                ]
            ],
        ),
    ],
    ids=["h3-and-b-in-capitals", "bold-title-without-h3"],
)
def test_notices_from_before_2007(kid, title, parts):
    """Uppercase <H3>/<B> markup, no document of their own; the address's second line has no label."""
    d = parse_one(page(f"one_{kid}"))
    assert (d["title"], d["parts"], d["address"] is not None) == (title, parts, True)


def test_title_in_capitals_reads_as_a_sentence():
    p = page("one_20230000696926").replace("Endring av kapital", "ENDRING AV KAPITAL")
    assert parse_one(p)["title"] == "Endring av kapital"


@pytest.mark.parametrize(
    "batch,status,read",
    [(0, 200, 0), (1, 200, 1), (4, 200, 4), (12, 200, 12), (13, 422, None), (-1, 422, None)],
    ids=["0-list-only", "1", "4", "12-max", "13-over-max", "negative"],
)
def test_page_chooses_how_many_to_read_per_call(app_client, w2, batch, status, read):
    """The page asks for a few at a time to draw as they come; never more than 12 per call."""
    resp = app_client.get(f"{URL}?batch={batch}")
    got = sum(it["read"] for it in resp.json()["items"]) if status == 200 else None
    assert (resp.status_code, got) == (status, read)


def test_small_calls_read_newest_first(app_client, w2):
    app_client.get(f"{URL}?batch=4")
    items = app_client.get(f"{URL}?batch=4").json()["items"]
    assert [it["read"] for it in items[:10]] == [True] * 8 + [False] * 2

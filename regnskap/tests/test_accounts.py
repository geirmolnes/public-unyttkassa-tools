"""Reading årsregnskap into figures, notes and text: API flow on a real filing (stored reads, no model calls),
checks, re-read choice, user-facing flags and the text helpers."""

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest
from pydantic import BaseModel

from src import db, usage, usage_client
from src.accounts import checks, llm, payload, pipeline, store
from src.accounts.llm import MissingKey, Reader, claude_cost, claude_request, strict_schema
from src.accounts.schema import Page, PageText
from src.accounts.text import assign_notes, interleave, text_numbers

FIX = Path(__file__).parent / "fixtures" / "accounts" / "921830327_2025"
ORGNR = "921830327"
# A real company's filing and the stored reads of it. The public copy of this repo is published without
# them: there the tests that read the PDF are skipped.
HAS_FILING = (FIX / "doc.pdf").exists()
PDF = (FIX / "doc.pdf").read_bytes() if HAS_FILING else b""
SHA = store.content_hash(PDF)
NO_FILING = "the real filing in tests/fixtures/accounts is not in this copy"
needs_filing = pytest.mark.skipif(not HAS_FILING, reason=NO_FILING)


def seed_reads() -> None:
    """The bench's stored reads of this PDF, as if an earlier run had made them."""
    for variant_dir in FIX.iterdir():
        if not variant_dir.is_dir():
            continue
        for f in variant_dir.glob("p-*.json"):
            store.put_read(
                SHA,
                int(f.stem[2:]),
                variant_dir.name,
                "anthropic/claude-haiku-5.5",
                "bench",
                False,
                0.0,
                json.loads(f.read_text()),
            )


@pytest.fixture
def restaurant(brreg):
    brreg.company(ORGNR, "18 RESTAURANT AS", [2024, 2025])
    brreg.pdf_bytes[(ORGNR, 2025)] = PDF


def wait(app_client, year: int = 2025) -> dict:
    for _ in range(500):
        rows = {
            r["year"]: r for r in app_client.get(f"/api/company/{ORGNR}/accounts").json()["years"]
        }
        if rows.get(year, {}).get("status") in ("ok", "failed"):
            return rows[year]
        time.sleep(0.02)
    raise AssertionError("read did not finish")


# ---------- API flow ----------


@needs_filing
def test_read_from_stored_reads_makes_no_model_calls(app_client, restaurant, reader):
    seed_reads()
    r = app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    assert (r.status_code, r.json()["queued"]) == (202, [2025])
    row = wait(app_client)
    assert (row["status"], row["pages_total"], row["pages_done"], reader.calls) == ("ok", 5, 5, [])


@needs_filing
def test_detail_of_a_read_year(app_client, restaurant):
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    d = app_client.get(f"/api/company/{ORGNR}/accounts/2025").json()
    res = d["statements"]["resultat"]
    assert res["lines"][:2] == [
        {
            "label": "Salgsinntekt",
            "ref": None,
            "key": None,
            "cur": 5801103,
            "prior": 5496135,
            "page": 2,
            "also": None,
        },
        {
            "label": "Sum inntekter",
            "ref": None,
            "key": "sum_driftsinntekter",
            "cur": 5801103,
            "prior": 5496135,
            "page": 2,
            "also": None,
        },
    ]
    assert (res["copy"], res["pages"], d["statements"]["balanse"]["pages"]) == (
        "brreg_printout",
        [2],
        [3, 4],
    )
    assert [(n["number"], n["title"], n["pages"]) for n in d["notes"]] == [
        ("1", "Antall årsverk i regnskapsåret", [5])
    ]
    # The only failing checks are numbers on the cover page (org.nr, journal number): not shown as flags
    assert d["checks"] == []
    assert d["doc"]["n_pages"] == 5


@needs_filing
def test_read_without_stored_reads_calls_each_model_once_per_page(app_client, restaurant, reader):
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    assert wait(app_client)["status"] == "ok"
    # fake pages are all 'annet', so every page also gets the text pass
    assert Counter(reader.calls) == {"Page": 5, "NumberList": 5, "PageText": 5}


@needs_filing
def test_model_page_images_are_removed_after_reading(app_client, restaurant):
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    assert list(store.page_cache_dir(SHA).glob("*.png")) == []


@needs_filing
def test_rereading_a_year_reuses_stored_calls(app_client, restaurant, reader):
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    assert len(reader.calls) == 15


def test_missing_api_keys_is_503_and_queues_nothing(app_client, restaurant, reader):
    reader.key = False
    r = app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    assert (r.status_code, app_client.get(f"/api/company/{ORGNR}/accounts").json()["years"]) == (
        503,
        [],
    )


@pytest.mark.parametrize("years", [[2023], [2026]], ids=["before-first", "after-last"])
def test_years_brreg_lacks_are_400(app_client, restaurant, years):
    assert (
        app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": years}).status_code == 400
    )


def test_empty_year_list_is_422(app_client, restaurant):
    assert app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": []}).status_code == 422


def test_year_already_queued_is_not_queued_again(app_client, restaurant):
    store.queue(ORGNR, 2025, "fake/model")
    r = app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2024, 2025]})
    assert r.json()["queued"] == [2024]


def test_detail_of_unread_year_is_404(app_client, restaurant):
    assert app_client.get(f"/api/company/{ORGNR}/accounts/2025").status_code == 404


@pytest.mark.parametrize("page,status", [(0, 404), (1, 200), (5, 200), (6, 404)])
@needs_filing
def test_page_image_bounds(app_client, restaurant, page, status):
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    r = app_client.get(f"/api/company/{ORGNR}/accounts/2025/pages/{page}")
    assert (r.status_code, r.headers["content-type"] if status == 200 else None) == (
        status,
        "image/webp" if status == 200 else None,
    )


@needs_filing
def test_stored_pdf_is_served_inline_unchanged(app_client, restaurant):
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    r = app_client.get(f"/api/company/{ORGNR}/accounts/2025/pdf")
    assert (r.headers["content-disposition"], r.content == PDF) == ("inline", True)


def test_company_view_lists_read_years(app_client, restaurant):
    store.queue(ORGNR, 2025, "fake/model")
    years = app_client.get(f"/api/company/{ORGNR}").json()["accounts"]
    assert [(y["year"], y["status"]) for y in years] == [(2025, "queued")]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_restart_leaves_unfinished_reads_to_be_resumed(status):
    """A deploy used to fail every read in progress ("Avbrutt av omstart"); pipeline.watch resumes them now."""
    store.queue(ORGNR, 2025, "fake/model")
    store.update(ORGNR, 2025, status=status)
    db.init_db()
    row = store.account_read(ORGNR, 2025)
    assert (row["status"], row["error"]) == (status, None)


def test_finished_reads_survive_restart():
    store.queue(ORGNR, 2025, "fake/model")
    store.finish(ORGNR, 2025, {}, [], 10)
    db.init_db()
    assert store.account_read(ORGNR, 2025)["status"] == "ok"


@needs_filing
def test_content_hash_ignores_the_trailer_id_brreg_changes_per_download():
    other_id = PDF.replace(b"/ID [<", b"/ID [<0", 1).replace(b"> <", b"> <0", 1)
    assert (other_id != PDF, store.content_hash(other_id)) == (True, SHA)


@needs_filing
def test_content_hash_changes_with_the_content():
    assert store.content_hash(PDF.replace(b"%PDF-1.", b"%PDF-2.", 1)) != SHA


@needs_filing
def test_a_new_download_with_another_trailer_id_reuses_stored_reads(
    app_client, restaurant, brreg, reader
):
    brreg.pdf_bytes[(ORGNR, 2025)] = PDF.replace(b"/ID [<", b"/ID [<0", 1).replace(
        b"> <", b"> <0", 1
    )
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    assert (wait(app_client)["status"], reader.calls) == ("ok", [])


# ---------- checks ----------


@pytest.mark.parametrize(
    "a,b,ok", [(100, 102, True), (100, 103, False), (100, 98, True), (100, 97, False)]
)
def test_close_tolerates_two_kroner(a, b, ok):
    assert checks.close(a, b) is ok


@pytest.mark.parametrize(
    "numbers,expected",
    [([9, 10], {10}), ([-10], {10}), ([1989, 1990], {1989}), ([2035, 2036], {2036}), ([], set())],
    ids=["min-listed", "negative", "first-year", "last-year", "empty"],
)
def test_listed_drops_small_numbers_and_years(numbers, expected):
    assert checks.listed(numbers) == expected


def test_listed_without_number_list_is_none():
    assert checks.listed(None) is None


def test_identity_needs_all_terms():
    vals = {"sum_eiendeler": (100, 90, 1, 2, "x")}
    assert checks.identity_ok(vals, [("sum_eiendeler", 1)], "sum_ek_gjeld", 0) is None


@pytest.mark.parametrize("cost", [40, -40], ids=["cost-positive", "cost-negative"])
def test_identity_accepts_either_sign_on_costs(cost):
    vals = {
        "sum_driftsinntekter": (100, None, 1, 2, ""),
        "sum_driftskostnad": (cost, None, 1, 2, ""),
        "driftsresultat": (60, None, 1, 2, ""),
    }
    terms = [("sum_driftsinntekter", 1), ("sum_driftskostnad", -1)]
    assert checks.identity_ok(vals, terms, "driftsresultat", 0) is True


# ---------- re-read choice ----------


def page(inline: list[tuple[str, int]] = (), kind: str = "noter") -> dict:
    return {
        "page_kind": kind,
        "is_brreg_printout": False,
        "figures_for": "selskap",
        "unit_scale": 1,
        "current_year": 2025,
        "prior_year": 2024,
        "statement_lines": [],
        "notes": [],
        "inline_amounts": [{"context": c, "value": v} for c, v in inline],
    }


NUMS = {1: [168000, 740]}


def test_choose_takes_the_reread_that_finds_the_missing_number():
    reads = {
        "base": {1: page([("Antall årsverk", 740)])},
        "fill": {1: page([("Antall årsverk", 740), ("Revisjonshonorar", 168000)])},
    }
    assert pipeline.choose(reads, NUMS, {1}) == {1: "fill"}


def test_choose_keeps_base_when_rereads_are_no_better():
    reads = {
        "base": {1: page([("Antall årsverk", 740)])},
        "think": {1: page([("Antall årsverk", 740)])},
    }
    assert pipeline.choose(reads, NUMS, {1}) == {}


@pytest.mark.parametrize(
    "context", ["Tall nevnt", "number list", "31 000", "honorar til revisor kr 31 000"]
)
def test_choose_rejects_rereads_that_invent_inline_amounts(context):
    reads = {
        "base": {1: page([("Antall årsverk", 740)])},
        "fill": {1: page([("Antall årsverk", 740), (context, 168000)])},
    }
    assert pipeline.choose(reads, NUMS, {1}) == {}


@pytest.mark.parametrize("context", ["Revisjonshonorar", "Antall aksjer", "H-tall"])
def test_real_inline_contexts_are_not_inventions(context):
    assert pipeline.invented_inline(page(), page([(context, 168000)])) is False


def stmt_page(kind: str, key: str | None, printout: bool = False) -> dict:
    line = {
        "label": "Sum eiendeler" if key else "Netto kontantstrøm",
        "note_ref": None,
        "key": key,
        "current": 100,
        "prior": 90,
    }
    return {**page(kind=kind), "is_brreg_printout": printout, "statement_lines": [line]}


@pytest.mark.parametrize("gap,shown", [(1, True), (3, True), (4, False)])
def test_cash_flow_page_after_the_statements_is_shown(gap, shown):
    """Regression: the run is picked by keyed totals; cash flow has none and was dropped (Stendi 2025, p. 23)."""
    doc = [(20, stmt_page("balanse", "sum_eiendeler")), (20 + gap, stmt_page("kontantstrom", None))]
    assert ("kontantstrom" in payload.statements(doc)) is shown


def test_a_kind_only_in_the_printout_is_shown_as_the_printout():
    """HENT 2025: the group balance sheet is only in Brreg's copy; it shows, labelled as the printout."""
    doc = [
        (20, stmt_page("balanse", "sum_eiendeler")),
        (21, stmt_page("kontantstrom", None, printout=True)),
    ]
    out = payload.statements(doc)
    assert (out["balanse"]["copy"], out["kontantstrom"]["copy"]) == (
        "company_copy",
        "brreg_printout",
    )


@pytest.mark.parametrize(
    "note,statement,same",
    [
        (1489252, 1489252000, True),
        (1489252, 1489253000, True),
        (1489252, 1489254001, False),
        (151245, 151245, True),
        (-12286, 12286000, True),
        (127275, 151245000, False),
    ],
    ids=["thousands", "thousands-rounded", "thousands-off", "equal", "sign", "different"],
)
def test_note_and_statement_amounts_match_across_a_missing_unit(note, statement, same):
    assert checks.same_amount(note, statement, 1000) is same


def bal(
    p: int, total: int, scale: int, who: str = "selskap", printout: bool = False
) -> tuple[int, dict]:
    lines = [
        {
            "label": "Sum eiendeler",
            "note_ref": None,
            "key": "sum_eiendeler",
            "current": total,
            "prior": 1,
        },
        {
            "label": "Sum egenkapital og gjeld",
            "note_ref": None,
            "key": "sum_ek_gjeld",
            "current": total,
            "prior": 1,
        },
    ]
    d = {
        **page(kind="balanse"),
        "figures_for": who,
        "is_brreg_printout": printout,
        "unit_scale": scale,
    }
    return p, {**d, "statement_lines": lines}


def test_group_statements_read_as_the_companys_are_relabelled():
    """HENT 2025: the group's own statements (NOK 1000) were read as 'selskap'; Brreg's konsern printout decides."""
    doc = [
        bal(3, 500_000, 1, printout=True),
        bal(6, 9_000_000, 1, "konsern", printout=True),
        bal(27, 9_000, 1000),
        bal(61, 500, 1000),
    ]
    assert [d["figures_for"] for _, d in checks.relabel_group_runs(doc)] == [
        "selskap",
        "konsern",
        "konsern",
        "selskap",
    ]


def test_without_a_group_printout_nothing_is_relabelled():
    doc = [bal(3, 500_000, 1, printout=True), bal(27, 9_000, 1000), bal(61, 500, 1000)]
    assert checks.relabel_group_runs(doc) == doc


# ---------- flags shown to the reader ----------

DOC = [(1, page(kind="annet")), (5, page([("Antall årsverk", 740)]))]
TEXTS = {
    5: {"blocks": [{"note": "3", "kind": "paragraph", "text": "Totalt innskudd er kr 8 608 984."}]}
}


@pytest.mark.parametrize(
    "failure",
    [
        {"kind": "omission", "value": 4512, "pages": [1]},
        {"kind": "unlisted", "value": 921830327, "pages": [5]},
        {"kind": "omission", "value": 8608984, "pages": [5]},
    ],
    ids=["cover-page", "own-orgnr", "number-in-text"],
)
def test_flags_skip_noise(failure):
    assert payload.reader_checks([failure], DOC, TEXTS, ORGNR) == []


def test_omissions_on_one_page_become_one_flag():
    fails = [
        {"kind": "omission", "value": 48, "pages": [5]},
        {"kind": "omission", "value": 574597, "pages": [5]},
    ]
    assert payload.reader_checks(fails, DOC, TEXTS, ORGNR) == [
        {
            "page": 5,
            "kind": "Ikke hentet ut",
            "short": None,
            "row": None,
            "values": [48, 574597],
            "note": None,
            "text": "48, 574 597 står på siden, men kom ikke med i tallene her. Se skannet.",
        }
    ]


def test_note_link_flag_points_at_the_note_page():
    doc = [(2, page(kind="resultat")), (28, page())]
    fail = {
        "kind": "note_link",
        "key": "aarsresultat",
        "row": "Årsresultat",
        "values": [-744099],
        "statement": [13994304, 23496579],
        "pages": [2, 28],
    }
    (flag,) = payload.reader_checks([fail], doc, {}, ORGNR)
    assert (flag["page"], flag["short"]) == (
        28,
        "Regnskapet sier 13 994 304. Sjekk om det er samme tall.",
    )


def note(number: str, pages: list[int], texts: list[str] = (), cells: list[int] = ()) -> dict:
    seq = [{"t": "text", "kind": "paragraph", "text": x, "page": pages[0]} for x in texts]
    if cells:
        seq.append(
            {
                "t": "table",
                "page": pages[0],
                "table": {"columns": [], "rows": [{"label": "r", "values": list(cells)}]},
            }
        )
    return {"number": number, "title": "", "pages": pages, "for": "selskap", "seq": seq}


@pytest.mark.parametrize(
    "notes,expected",
    [
        ([note("8", [31], cells=[574598]), note("9", [31], ["garanti på kr 18 mill"])], 1),
        ([note("8", [31], cells=[18000000]), note("9", [31])], 0),
        ([note("9", [31])], 0),
        ([note("8", [31]), note("9", [31])], None),
    ],
    ids=["text-in-millions", "table-cell", "only-note-on-page", "no-note-has-it"],
)
def test_flag_goes_to_the_note_holding_its_number(notes, expected):
    """Regression: a flag on a page with two notes counted on both (Stendi 2025 p. 31, notes 8 and 9)."""
    flags = [{"page": 31, "values": [18000000]}]
    payload.attach_notes(flags, notes)
    assert flags[0]["note"] == expected


# ---------- text helpers ----------


def b(note, text="x", kind="paragraph"):
    return {"note": note, "kind": kind, "text": text}


def test_continuation_gets_previous_pages_open_note():
    out = assign_notes(
        [[b("3", kind="heading"), b("4", kind="heading")], [b(None), b("5", kind="heading")]],
        ["noter", "noter"],
    )
    assert ([x["note"] for x in out[1]], out[1][0].get("continued")) == (["4", "5"], True)


def test_no_carry_over_across_a_non_notes_page():
    out = assign_notes([[b("9")], [b(None)], [b(None)]], ["noter", "beretning", "noter"])
    assert [out[1][0]["note"], out[2][0]["note"]] == [None, None]


def t(label):
    return {"columns": [], "rows": [{"label": label, "values": []}]}


def test_interleave_places_tables_at_their_placeholders():
    blocks = [
        b("3", "Note 3", "heading"),
        b("3", "Lønninger", "table"),
        b("3", "Tekst"),
        b("3", "Lovpålagt revisjon", "table"),
    ]
    assert interleave(blocks, [t("Lønninger"), t("Lovpålagt revisjon")]) == [
        ("text", 0),
        ("table", 0),
        ("text", 2),
        ("table", 1),
    ]


def test_interleave_keeps_unmarked_tables_in_read_order():
    tables = [t("Betalbar skatt"), t("Resultat før skatt"), t("Varige driftsmidler")]
    blocks = [b("7", "Betalbar skatt", "table"), b("7", "Varige driftsmidler", "table")]
    assert interleave(blocks, tables) == [("table", 0), ("table", 1), ("table", 2)]


def test_interleave_appends_tables_without_any_placeholder():
    assert interleave([b(None, "Tekst"), b(None, "Finnes ikke", "table")], [t("A")]) == [
        ("text", 0),
        ("table", 0),
    ]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("kr 8 608 984 eksklusiv", [8608984]),
        ("kr 18 mill", [18]),
        ("638,8 millioner", [638]),
        ("pr 31.12.2025", [31]),
        ("1.234.567 kroner", [1234567]),
        ("ingen tall", []),
    ],
)
def test_text_numbers(text, expected):
    assert text_numbers(text) == expected


# ---------- request sent to the Claude API ----------


def test_strict_schema_keeps_field_named_title():
    note = strict_schema(Page)["$defs"]["Note"]
    assert (sorted(note["properties"]), note["required"]) == (
        ["number", "tables", "title"],
        ["number", "title", "tables"],
    )


def test_strict_schema_requires_defaulted_field():
    s = strict_schema(Page)
    assert ("inline_amounts" in s["required"], "default" in s["properties"]["inline_amounts"]) == (
        True,
        False,
    )


def test_strict_schema_closes_text_block():
    assert strict_schema(PageText)["$defs"]["TextBlock"]["additionalProperties"] is False


class Box(BaseModel):
    n: int


@pytest.mark.parametrize(
    "reasoning,thinking,effort",
    [("none", {"type": "disabled"}, None), ("low", {"type": "adaptive"}, "low")],
)
def test_claude_request_thinking(reasoning, thinking, effort):
    r = claude_request("claude-haiku-5-5", b"png", "p", Box, reasoning)
    # Haiku 5.5 rejects temperature=0 with a 400
    assert (r["thinking"], r["output_config"].get("effort"), "temperature" in r) == (
        thinking,
        effort,
        False,
    )


@pytest.mark.parametrize(
    "prompt_tokens,cost",
    [(99_999, 0.0104999), (100_000, 0.0105), (100_001, 0.0525005)],
    ids=["below", "at", "above"],
)
def test_claude_cost_switches_rate_card_above_100k_prompt_tokens(prompt_tokens, cost):
    assert claude_cost(prompt_tokens, 1000) == pytest.approx(cost)


def claude_message(stop_reason: str = "end_turn") -> anthropic.types.Message:
    """Haiku 5.5 thinks by default, so the answer comes after an (empty) thinking block."""
    return anthropic.types.Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": '{"n": 7}'},
            ],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 2000, "output_tokens": 500},
        }
    )


class FakeClaude:
    def __init__(self, msg: anthropic.types.Message) -> None:
        self.msg, self.calls, self.messages = msg, 0, self

    async def create(self, **kwargs) -> anthropic.types.Message:
        self.calls += 1
        return self.msg


def fake_gemini(text: str) -> SimpleNamespace:
    async def generate_content(**kwargs):
        usage = SimpleNamespace(prompt_token_count=1000, candidates_token_count=100)
        return SimpleNamespace(text=text, usage_metadata=usage)

    return SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )


@pytest.fixture
def claude_reader(monkeypatch):
    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    return Reader()


async def test_claude_read_parses_answer_after_thinking_block(claude_reader):
    claude_reader._claude = FakeClaude(claude_message())
    parsed, usage = await claude_reader.read(b"png", "p", Box)
    assert (parsed, usage.model, usage.fallback, usage.cost) == (
        Box(n=7),
        "claude-haiku-5-5",
        False,
        pytest.approx(0.00045),
    )


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_unfinished_claude_answer_falls_back_to_gemini(claude_reader, stop_reason):
    claude = FakeClaude(claude_message(stop_reason))
    claude_reader._claude, claude_reader._gemini = claude, fake_gemini('{"n": 3}')
    parsed, usage = await claude_reader.read(b"png", "p", Box)
    assert (parsed, usage.model, usage.fallback, claude.calls) == (
        Box(n=3),
        "gemini-3.5-flash-lite",
        True,
        3,
    )


@pytest.mark.parametrize(
    "keys,chain",
    [
        ({"ANTHROPIC_API_KEY", "GEMINI_API_KEY"}, ["claude-haiku-5-5", "gemini-3.5-flash-lite"]),
        ({"ANTHROPIC_API_KEY"}, ["claude-haiku-5-5"]),
        ({"GEMINI_API_KEY"}, ["gemini-3.5-flash-lite"]),
    ],
    ids=["both", "claude-only", "gemini-only"],
)
def test_chain_follows_configured_keys(monkeypatch, keys, chain):
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(k, "test") if k in keys else monkeypatch.delenv(k, raising=False)
    assert Reader().chain() == chain


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    """Usage reporting switched on, into a local outbox that is never sent."""
    path = tmp_path / "outbox.sqlite"
    monkeypatch.setenv("AI_USAGE_TOKEN", "test")
    monkeypatch.setenv("AI_USAGE_OUTBOX", str(path))
    monkeypatch.setenv("AI_USAGE_BILLING", "paid")
    monkeypatch.setattr(usage_client, "flush", lambda: None)

    def events() -> list[dict]:
        if not path.exists():
            return []
        with sqlite3.connect(path) as db_:
            return [
                json.loads(r[0]) for r in db_.execute("SELECT payload FROM outbox ORDER BY rowid")
            ]

    return events


def summary(e: dict) -> tuple:
    return (
        e.get("provider"),
        e["model"],
        e["operation"],
        e["kind"],
        e["modality"],
        e["status"],
        e["input_tokens"],
        e["output_tokens"],
    )


async def test_claude_page_read_is_reported(claude_reader, outbox):
    claude_reader._claude = FakeClaude(claude_message())
    await claude_reader.read(b"png", "p", Box)
    assert [summary(e) for e in outbox()] == [
        ("anthropic", "claude-haiku-5-5", "page-read", "worker", "image", "ok", 2000, 500)
    ]


async def test_refused_page_read_reports_every_attempt_and_the_fallback(claude_reader, outbox):
    claude_reader._claude, claude_reader._gemini = (
        FakeClaude(claude_message("refusal")),
        fake_gemini('{"n": 3}'),
    )
    await claude_reader.read(b"png", "p", Box)
    assert [summary(e) for e in outbox()] == [
        ("anthropic", "claude-haiku-5-5", "page-read", "worker", "image", "ok", 2000, 500),
    ] * 3 + [(None, "gemini-3.5-flash-lite", "page-read", "worker", "image", "ok", 1000, 100)]


async def test_failed_claude_call_is_reported_without_counts(claude_reader, outbox):
    class Down(FakeClaude):
        async def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=None)

    claude_reader._claude, claude_reader._gemini = Down(claude_message()), fake_gemini('{"n": 3}')
    await claude_reader.read(b"png", "p", Box)
    assert [(e.get("provider"), e["status"], e["input_tokens"]) for e in outbox()] == [
        ("anthropic", "error", None)
    ] * 3 + [(None, "ok", 1000)]


def test_claude_event_counts_cache_reads_and_writes_as_prompt():
    msg = SimpleNamespace(
        model="claude-haiku-5-5",
        usage=SimpleNamespace(
            input_tokens=1000,
            cache_read_input_tokens=300,
            cache_creation_input_tokens=200,
            output_tokens=50,
        ),
    )
    e = usage.claude_event(msg, "claude-haiku-5-5", "page-read", "worker", "image")
    assert (e["input_tokens"], e["cached_tokens"], e["output_tokens"], e["total_tokens"]) == (
        1500,
        300,
        50,
        1550,
    )


async def test_nothing_is_reported_without_a_token(claude_reader, outbox, monkeypatch):
    monkeypatch.delenv("AI_USAGE_TOKEN")
    claude_reader._claude = FakeClaude(claude_message())
    await claude_reader.read(b"png", "p", Box)
    assert outbox() == []


def test_chain_without_keys_names_the_missing_ones(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(MissingKey, match="ANTHROPIC_API_KEY og GEMINI_API_KEY"):
        Reader().chain()


# ---------- konsern ----------


def konsern_balance(ek_gjeld: int) -> dict:
    lines = [
        {
            "label": "Sum eiendeler",
            "note_ref": None,
            "key": "sum_eiendeler",
            "current": 100,
            "prior": 80,
        },
        {
            "label": "Sum egenkapital og gjeld",
            "note_ref": None,
            "key": "sum_ek_gjeld",
            "current": ek_gjeld,
            "prior": 80,
        },
    ]
    return {**page(kind="balanse"), "figures_for": "konsern", "statement_lines": lines}


@pytest.mark.parametrize(
    "ek_gjeld,failing",
    [(100, []), (102, []), (103, ["section", "identity"])],
    ids=["equal", "within-2", "off-by-3"],
)
def test_group_balance_sheet_is_checked(ek_gjeld, failing):
    fails, _ = pipeline.failures([(40, konsern_balance(ek_gjeld))], {})
    assert [(f["kind"], f["scope"]) for f in fails] == [(k, "konsern") for k in failing]


def test_group_statements_are_shown_apart_from_the_company():
    doc = [(20, stmt_page("balanse", "sum_eiendeler")), (40, konsern_balance(100))]
    out = (payload.statements(doc), payload.statements(pipeline.scoped(doc, "konsern")))
    assert (
        [(k, v["pages"]) for k, v in out[0].items()],
        [(k, v["pages"]) for k, v in out[1].items()],
    ) == (
        [("balanse", [20])],
        [("balanse", [40])],
    )


# ---------- page budget per click ----------


@pytest.mark.parametrize(
    "n_new,left,fits",
    [(999, 1000, True), (1000, 1000, True), (1001, 1000, False), (5, 4, False), (0, 0, True)],
)
def test_page_budget_boundaries(n_new, left, fits):
    assert (pipeline.over_budget(n_new, [left]) is None) is fits


def test_over_the_limit_says_why():
    assert pipeline.over_budget(1001, [1000]) == (
        "Ikke lest: 1001 sider er mer enn grensen på 1000 sider per innlesing."
    )


@needs_filing
def test_year_over_the_budget_is_not_read(app_client, restaurant, reader, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_PAGES_PER_CLICK", 4)
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    row = wait(app_client)
    assert (row["status"], row["error"], reader.calls) == (
        "failed",
        "Ikke lest: 5 sider er mer enn grensen på 4 sider per innlesing.",
        [],
    )


@needs_filing
def test_stored_pages_do_not_count_against_the_budget(app_client, restaurant, reader, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_PAGES_PER_CLICK", 0)
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    assert wait(app_client)["status"] == "ok"


@needs_filing
def test_second_year_waits_when_the_first_used_the_budget(app_client, brreg, reader, monkeypatch):
    brreg.company(ORGNR, "18 RESTAURANT AS", [2024, 2025])
    brreg.pdf_bytes[(ORGNR, 2025)] = PDF
    brreg.pdf_bytes[(ORGNR, 2024)] = PDF.replace(
        b"%PDF-1.", b"%PDF-2.", 1
    )  # same pages, other content hash
    monkeypatch.setattr(pipeline, "MAX_PAGES_PER_CLICK", 5)
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2024, 2025]})
    rows = (wait(app_client, 2025), wait(app_client, 2024))
    assert [(r["year"], r["status"]) for r in rows] == [(2025, "ok"), (2024, "failed")]


# ---------- search ----------


@pytest.fixture
def searchable(app_client, restaurant):
    if not HAS_FILING:
        pytest.skip(NO_FILING)
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    return app_client


def search(client, q: str) -> dict:
    return client.get(f"/api/company/{ORGNR}/accounts/search", params={"q": q}).json()


def test_search_finds_running_text_with_page(searchable):
    hit = search(searchable, "regnskapslovens bestemmelser")["text"][0]
    assert (hit["year"], hit["page"], "\x01" in hit["snippet"]) == (2025, 5, True)


def test_search_matches_word_prefixes(searchable):
    assert search(searchable, "regnskapslov")["text"][0]["page"] == 5


@pytest.mark.parametrize("q", ["5 801 103", "5801103", "−5 801 103"])
def test_search_finds_an_amount_however_it_is_written(searchable, q):
    hits = search(searchable, q)["figures"]
    assert {(h["label"], h["page"]) for h in hits} >= {("Salgsinntekt", 2), ("Sum inntekter", 2)}


def test_search_finds_figure_labels(searchable):
    assert {h["page"] for h in search(searchable, "salgsinntekt")["figures"]} == {2}


@pytest.mark.parametrize("q", ["a", " b "], ids=["one-char", "one-char-padded"])
def test_search_needs_two_characters(searchable, q):
    assert search(searchable, q) == {"q": q.strip(), "text": [], "figures": []}


@pytest.mark.parametrize("q", ['"', "NEAR(a b", "a OR", "*", "regnskap^2"])
def test_search_survives_fts_syntax(searchable, q):
    assert (
        searchable.get(f"/api/company/{ORGNR}/accounts/search", params={"q": q}).status_code == 200
    )


def test_chat_gets_matching_passages_from_the_filings(searchable, ai):
    searchable.post(
        "/api/chat",
        json={
            "orgnr": ORGNR,
            "messages": [
                {"role": "user", "content": "Hva sier regnskapsprinsippene om varige driftsmidler?"}
            ],
        },
    )
    assert "UTDRAG FRA ÅRSREGNSKAPENE" in ai.systems[-1] and "[2025 s. 5" in ai.systems[-1]


# ---------- identifiers are not misreads ----------


@pytest.mark.parametrize(
    "heading,skipped",
    [
        ("Organisasjonsnummer", True),
        ("Org.nr", True),
        ("Org. nr.", True),
        ("org nummer", True),
        ("Organization number", True),
        ("Kontonummer", True),
        ("Bokført verdi", False),
        ("Antall", False),
    ],
)
def test_identifier_columns_are_known_by_their_heading(heading, skipped):
    d = {
        **page(),
        "notes": [
            {
                "number": "13",
                "title": "x",
                "tables": [
                    {
                        "columns": ["Selskap", heading],
                        "rows": [
                            {"label": "A AS", "role": "item", "key": None, "values": [930861642]}
                        ],
                    }
                ],
            }
        ],
    }
    assert (930861642 in checks.identifier_values(d)) is skipped


def test_inline_org_number_is_skipped_by_its_label():
    assert checks.identifier_values(page([("Organisasjonsnummer 918 561 854", 918561854)])) == {
        918561854
    }


def test_an_amount_that_looks_like_an_org_number_is_still_checked():
    """930 861 642 passes the org-number check digit, but under 'Bokført verdi' it is an amount: keep checking it."""
    d = {
        **page(),
        "notes": [
            {
                "number": "5",
                "title": "Aksjer",
                "tables": [
                    {
                        "columns": ["Selskap", "Bokført verdi"],
                        "rows": [
                            {"label": "A AS", "role": "item", "key": None, "values": [930861642]}
                        ],
                    }
                ],
            }
        ],
    }
    assert checks.unlisted(d, [28]) == {930861642}


def subsidiaries_page() -> dict:
    """A subsidiaries table: names and org numbers (which the number list leaves out on purpose)."""
    rows = [
        {
            "label": "Læringsverkstedet Alversund Barnehage AS",
            "role": "item",
            "key": None,
            "values": [930861642],
        },
        {
            "label": "Læringsverkstedet Aronien Barnehage AS",
            "role": "item",
            "key": None,
            "values": [930874299],
        },
    ]
    return {
        **page(),
        "notes": [
            {
                "number": "13",
                "title": "Datterselskap",
                "tables": [{"columns": ["Selskapsnavn", "Organisasjonsnummer"], "rows": rows}],
            }
        ],
    }


def test_org_numbers_in_a_note_table_are_not_flagged_as_misreads():
    """Regression: every row of Læringsverkstedet's 2025 subsidiaries note was flagged 'Mulig feillest'."""
    fails, _ = checks.doc_failures([(21, subsidiaries_page())], {21: [28]})
    assert [f for f in fails if f["kind"] == "unlisted"] == []


def test_stored_org_number_flags_are_hidden_without_rereading():
    fails = [
        {"kind": "unlisted", "value": 930861642, "pages": [21]},
        {"kind": "unlisted", "value": 18000000, "pages": [21]},
    ]
    flags = payload.reader_checks(fails, [(21, subsidiaries_page())], {}, ORGNR)
    assert [f["values"] for f in flags] == [[18000000]]


# ---------- units printed once per statement ----------


def stmt(
    p: int,
    scale: int,
    lines: list[tuple[str, str | None, int]],
    kind: str = "balanse",
    printout: bool = False,
):
    sl = [
        {"label": lbl, "note_ref": None, "key": key, "current": v, "prior": None}
        for lbl, key, v in lines
    ]
    return p, {
        **page(kind=kind),
        "is_brreg_printout": printout,
        "unit_scale": scale,
        "statement_lines": sl,
    }


PRINTOUT = stmt(
    3,
    1,
    [
        ("Sum eiendeler", "sum_eiendeler", 4460000),
        ("Sum egenkapital og gjeld", "sum_ek_gjeld", 4460000),
    ],
    printout=True,
)


def scales(doc) -> list[int]:
    return [d["unit_scale"] for _, d in checks.fix_unit_scales(doc)]


def test_page_without_unit_takes_the_runs_thousands_when_the_printout_agrees():
    """Regression: Læringsverkstedet 2025 printed 'NOK 1000' only on p. 10; pp. 11–12 were read as kroner and all 34
    lines differed from Brreg's printout by ×1000."""
    doc = [
        PRINTOUT,
        stmt(10, 1000, [("Sum driftsinntekter", "sum_driftsinntekter", 7150)], kind="resultat"),
        stmt(
            11,
            1,
            [
                ("Sum eiendeler", "sum_eiendeler", 4460),
                ("Sum egenkapital og gjeld", "sum_ek_gjeld", 4460),
            ],
        ),
    ]
    assert scales(doc) == [1, 1000, 1000]


def test_page_in_kroner_that_matches_the_printout_keeps_its_unit():
    doc = [
        PRINTOUT,
        stmt(10, 1000, [("Sum driftsinntekter", "sum_driftsinntekter", 7150)], kind="resultat"),
        stmt(
            11,
            1,
            [
                ("Sum eiendeler", "sum_eiendeler", 4460000),
                ("Sum egenkapital og gjeld", "sum_ek_gjeld", 4460000),
            ],
        ),
    ]
    assert scales(doc) == [1, 1000, 1]


def test_cash_flow_without_shared_lines_takes_the_runs_unit():
    doc = [
        PRINTOUT,
        stmt(10, 1000, [("Sum driftsinntekter", "sum_driftsinntekter", 7150)], kind="resultat"),
        stmt(13, 1, [("Resultat før skattekostnad", None, 72123)], kind="kontantstrom"),
    ]
    assert scales(doc) == [1, 1000, 1000]


def test_cash_flow_far_from_the_run_keeps_its_unit():
    doc = [
        stmt(10, 1000, [("Sum driftsinntekter", "sum_driftsinntekter", 7150)], kind="resultat"),
        stmt(14, 1, [("Resultat før skattekostnad", None, 72123)], kind="kontantstrom"),
    ]
    assert scales(doc) == [1000, 1]


def test_without_a_printout_the_run_decides():
    doc = [
        stmt(10, 1000, [("Sum driftsinntekter", "sum_driftsinntekter", 7150)], kind="resultat"),
        stmt(11, 1, [("Sum eiendeler", "sum_eiendeler", 4460)]),
    ]
    assert scales(doc) == [1000, 1000]


# ---------- sums in the statements ----------


def test_netto_endring_line_item_is_not_checked_as_a_sum():
    """Regression: 'Netto endring i øvrige langsiktige fordringer' (-2 020) was flagged as not the sum of the
    investing lines above it."""
    lines = [
        ("Netto kontantstrøm fra operasjonelle aktiviteter", None, 663657),
        ("Innbetalinger ved salg av varige driftsmidler", None, 7424),
        ("Investeringer i varige driftsmidler", None, -61691),
        ("Investeringer i immaterielle eiendeler", None, -36781),
        ("Oppkjøp av datterselskap, fratrukket kontantbeholdning", None, -38736),
        ("Salg av datterselskap, fratrukket kontantbeholdning", None, 0),
        ("Investeringer i finansielle omløpsmidler", None, -20000),
        ("Netto endring i øvrige langsiktige fordringer", None, -2020),
        ("Netto kontantstrøm fra investeringsaktiviteter", None, -151804),
    ]
    results = checks.section_results([stmt(12, 1, lines, kind="kontantstrom")])
    assert [(label, ok) for label, _, _, ok in results] == [
        ("Netto kontantstrøm fra investeringsaktiviteter", True)
    ]


@pytest.mark.parametrize(
    "result,ok",
    [(-244608, True), (244608, True), (-244611, False)],
    ids=["minus-costs", "costs", "off-by-3"],
)
def test_operating_result_without_income_is_minus_the_costs(result, ok):
    """Regression: HJR Holding 2022, last year's column: no income, Driftsresultat −244 608 = −Sum kostnader was
    flagged as not the sum of the lines above."""
    lines = [
        ("Salgsinntekt", None, 0),
        ("Sum inntekter", None, 0),
        ("Annen driftskostnad", None, 244608),
        ("Sum kostnader", None, 244608),
        ("Driftsresultat", None, result),
    ]
    results = checks.section_results([stmt(2, 1, lines, kind="resultat")])
    assert [(label, r) for label, _, _, r in results] == [
        ("Sum kostnader", True),
        ("Driftsresultat", ok),
    ]


@pytest.mark.parametrize(
    "net_change,ok",
    [(-345493, True), (-345496, False)],
    ids=["nets-plus-fx", "off-by-3"],
)
def test_net_change_in_cash_counts_the_line_after_the_last_net(net_change, ok):
    """Regression: HJR Holding 2022 (konsern): net change = the three nets + 'Valutaeffekt', a plain line between
    the financing net and the total, was flagged."""
    lines = [
        ("Resultat før skattekostnad", None, -133794),
        ("Avskrivninger", None, 311220),
        ("Endring i andre tidsavgrensningsposter", None, -120800),
        ("Netto kontantstrøm fra operasjonelle aktiviteter", None, 12709),
        ("Innbetalinger ved salg av varige driftsmidler", None, 261394),
        ("Utbetalinger ved kjøp av varige driftsmidler", None, -193859),
        ("Netto utbetaling ved kjøp av datterselskap", None, -77279),
        ("Netto kontantstrøm fra investeringsaktiviteter", None, -23009),
        ("Innbetalinger ved opptak av ny langsiktig gjeld", None, 87639),
        ("Innfrielse av gjeld", None, -341302),
        ("Utbetalinger ved nedbetaling av langsiktig gjeld", None, -64629),
        ("Netto endring kassekreditt", None, 9941),
        ("Utbetalinger av utbytte / andre EK effekter", None, -30300),
        ("Netto kontantstrøm fra finansieringsaktiviteter", None, -338651),
        ("Valutaeffekt utenlandske enheter", None, 3458),
        ("Netto endring i kontanter og kontantekv.", None, net_change),
    ]
    doc = [stmt(25, 1, lines, kind="kontantstrom")]
    results = {label: r for label, _, _, r in checks.section_results(doc)}
    assert results["Netto endring i kontanter og kontantekv."] is ok


def test_result_split_between_owners_is_not_a_sum():
    """Regression: HJR Holding 2022 (konsern): 'Sum andel majoritet' = Årsresultat − the minority line below it."""
    lines = [
        ("Ordinært resultat før skattekostnad", None, -133794),
        ("Skattekostnad på ordinært resultat", None, 29923),
        ("Årsresultat", None, -163717),
        ("Sum andel majoritet", None, -163811),
        ("Sum andel minoritet", None, 94),
    ]
    results = checks.section_results([stmt(22, 1, lines, kind="resultat")])
    assert [(label, ok) for label, _, _, ok in results] == [("Årsresultat", True)]


@pytest.mark.parametrize(
    "label,computed",
    [
        ("Netto endring i kontanter og kontantekvivalenter", True),
        ("Netto endring i likvider", True),
        ("Netto endring i bankinnskudd, kontanter og lignende", True),
        ("Netto endring likvide midler", True),
        ("Netto økning i betalingsmidler", True),
        ("Net increase (decrease) in cash and cash equivalents", True),
        ("Net cash from operating activities", True),
        ("Netto endring i øvrige langsiktige fordringer", False),
        ("Netto endring i kassekreditt", False),
        ("Net change in other receivables", False),
    ],
)
def test_computed_line_is_only_a_cash_total(label, computed):
    assert bool(checks.COMPUTED_LINE.search(label)) is computed


# ---------- Brreg's figures for the year, and last year's filing ----------


def brreg_row(year: int, sum_eiendeler: int, aarsresultat: int, kind: str = "SELSKAP") -> dict:
    """One /regnskap row as Brreg sends it (only the fields these tests need)."""
    return {
        "regnskapstype": kind,
        "valuta": "NOK",
        "regnskapsperiode": {"fraDato": f"{year}-01-01", "tilDato": f"{year}-12-31"},
        "eiendeler": {"sumEiendeler": sum_eiendeler},
        "resultatregnskapResultat": {"aarsresultat": aarsresultat},
    }


def test_reference_maps_brreg_fields_to_statement_keys():
    kf = {"sum_eiendeler": 5, "egenkapital": 3, "goodwill": 1, "aarsresultat": None}
    assert (checks.reference(kf), checks.reference(None)) == ({"sum_eiendeler": 5, "sum_ek": 3}, {})


@pytest.mark.parametrize(
    "brreg,ok",
    [
        (1_234_000, True),
        (1_235_000, True),
        (1_233_000, True),
        (1_235_001, False),
        (1_232_999, False),
    ],
    ids=["equal", "rounding-edge-up", "rounding-edge-down", "one-over", "one-under"],
)
def test_read_in_thousands_is_checked_against_brreg_with_rounding(brreg, ok):
    """1 234 read in NOK 1000 matches Brreg's NOK figure within one thousand either way."""
    fails, n = checks.doc_failures(
        [bal(3, 1234, 1000, printout=True)], {}, {"sum_eiendeler": brreg}
    )
    found = [
        (f["kind"], f["key"], f["values"], f["brreg"], f["pages"])
        for f in fails
        if f["kind"] == "brreg_data"
    ]
    expected = [] if ok else [("brreg_data", "sum_eiendeler", [1_234_000], brreg, {3})]
    assert (found, n > 0) == (expected, True)


def test_without_brreg_figures_nothing_is_compared():
    with_ref, n_ref = checks.doc_failures(
        [bal(3, 1234, 1000, printout=True)], {}, {"sum_eiendeler": 1}
    )
    without, n = checks.doc_failures([bal(3, 1234, 1000, printout=True)], {}, None)
    assert ([f["kind"] for f in without], n_ref - n) == ([], 1)


@pytest.mark.parametrize(
    "before,differs",
    [(1000, False), (1500, False), (5000, True)],
    ids=["same", "rounding", "restated"],
)
def test_last_years_column_against_last_years_filing(before, differs):
    """Prior column 1 (NOK 1000) against last year's 1 000 / 1 500 / 5 000 kroner."""
    diffs = checks.prior_differences([bal(3, 1234, 1000, printout=True)], {"sum_eiendeler": before})
    assert diffs == (
        [
            {
                "key": "sum_eiendeler",
                "label": "Sum eiendeler",
                "read": 1000,
                "before": before,
                "page": 3,
            }
        ]
        if differs
        else []
    )


@needs_filing
def test_detail_checks_brreg_figures_and_shows_restated_prior_as_information(
    app_client, restaurant, brreg
):
    """2025 Sum eiendeler off by 5 000 against Brreg: a flag. 2024 årsresultat in Brreg differs from this filing's
    prior column: information only, not counted as a check."""
    brreg.figures[ORGNR] = [brreg_row(2025, 1_837_024, 76805), brreg_row(2024, 1_361_483, 400_000)]
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    d = app_client.get(f"/api/company/{ORGNR}/accounts/2025").json()
    assert [(c["kind"], c["page"], c["row"]) for c in d["checks"]] == [
        ("Ulikt Brregs tall", 3, "SUM EIENDELER")
    ]
    assert [(i["kind"], i["page"], i["text"]) for i in d["info"]] == [
        (
            "Ulikt fjorårets regnskap",
            2,
            "«Årsresultat» for 2024 står som 390 125 her, men 400 000 i regnskapet for 2024. "
            "Tallene kan være omarbeidet; se notene.",
        )
    ]


@needs_filing
def test_detail_matching_brreg_has_no_flags_or_information(app_client, restaurant, brreg):
    brreg.figures[ORGNR] = [brreg_row(2025, 1_832_024, 76805), brreg_row(2024, 1_361_483, 390_125)]
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    d = app_client.get(f"/api/company/{ORGNR}/accounts/2025").json()
    assert (d["checks"], d["info"]) == ([], [])


@needs_filing
def test_our_read_of_a_year_gives_its_totals_for_next_years_comparison(app_client, restaurant):
    """Without Brreg figures for last year, last year's own read stands in (printout first)."""
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    got = store.statement_totals(ORGNR, 2025)
    assert (
        {k: got[k] for k in ("sum_eiendeler", "aarsresultat")},
        store.statement_totals(ORGNR, 2024),
    ) == (
        {"sum_eiendeler": 1832024, "aarsresultat": 76805},
        {},
    )


@needs_filing
def test_figures_of_a_read_year_brreg_lacks(app_client, restaurant):
    seed_reads()
    app_client.post(f"/api/company/{ORGNR}/accounts", json={"years": [2025]})
    wait(app_client)
    sel = app_client.get(f"/api/company/{ORGNR}/accounts/figures").json()["selskap"]
    e = next(e for e in sel["years"] if e["year"] == 2025)
    assert (
        e["pdf"],
        e["sum_eiendeler"] > 0,
        e["checks"]["sum_eiendeler"]["src"][0]["col"],
        sel["brreg"],
    ) == (True, True, "current", {})


def test_figures_without_reads_are_empty(app_client, restaurant):
    assert app_client.get(f"/api/company/{ORGNR}/accounts/figures").json() == {
        "selskap": {"years": [], "brreg": {}},
        "konsern": {"years": [], "brreg": {}},
    }

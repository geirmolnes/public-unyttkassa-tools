"""Company view with roles/figures, subsidiaries extraction → owned_by, and chat (AI faked)."""

import json
import time
from pathlib import Path

import pytest

from src.subsidiaries import Extraction, SubsidiaryRow

FIXTURES = Path(__file__).parent / "fixtures"


def _wait(app_client, orgnr: str) -> dict:
    for _ in range(200):
        data = app_client.get(f"/api/company/{orgnr}/subsidiaries").json()
        if data["status"] != "running":
            return data
        time.sleep(0.02)
    raise AssertionError("extraction did not finish")


@pytest.fixture
def hjr(brreg, ai):
    brreg.company("998812623", "HJR HOLDING AS", [2024, 2025])
    brreg.company("998831067", "DIBBER AS", [2025])
    brreg.figures["998812623"] = json.loads((FIXTURES / "hjr_regnskap_2025.json").read_text())
    brreg.roles["998812623"] = json.loads((FIXTURES / "hjr_roller.json").read_text())
    brreg.search_rows = [
        {"organisasjonsnummer": "998831067", "navn": "DIBBER AS"},
        {"organisasjonsnummer": "923991832", "navn": "DIBBER INDIA AS"},
    ]
    ai.extraction = Extraction(
        note_found=True,
        rows=[
            SubsidiaryRow(
                name="Dibber AS", country="Norge", place="Jessheim", share_pct=100, page=36
            ),
            SubsidiaryRow(
                name="Pilke Oy", country="Finland", place="Helsinki", share_pct=100, page=40
            ),
            SubsidiaryRow(
                name="Ukjent Navn AS", country="Norge", place="Bergen", share_pct=100, page=36
            ),
        ],
    )
    return brreg


def test_company_view_has_roles_and_full_figures(app_client, hjr):
    body = app_client.get("/api/company/998812623").json()
    assert (
        [r["role"] for r in body["company"]["roles"]],
        body["key_figures"]["skattekostnad_avledet"],
        body["key_figures"]["morselskap"],
    ) == (["Styrets leder", "Revisor", "Regnskapsfører"], 7447000.0, True)


def test_company_view_survives_missing_roles(app_client, hjr):
    assert app_client.get("/api/company/998831067").json()["company"]["roles"] == []


def test_parent_autostarts_extraction_and_child_shows_owned_by(app_client, hjr, ai):
    first = app_client.get("/api/company/998812623").json()["ownership"]["subsidiaries"]
    assert first["status"] in ("running", "ok")
    subs = _wait(app_client, "998812623")
    assert (subs["status"], subs["year"], [(r["name"], r["orgnr"]) for r in subs["rows"]]) == (
        "ok",
        2025,
        [("DIBBER AS", "998831067"), ("Pilke Oy", None), ("Ukjent Navn AS", None)],
    )
    assert ai.pdfs == [b"%PDF-1.4 fake"] and hjr.pdf_requests == [("998812623", 2025)]
    child = app_client.get("/api/company/998831067").json()["ownership"]["owned_by"]
    assert child == [
        {
            "orgnr": "998812623",
            "name": "HJR HOLDING AS",
            "share_pct": 100.0,
            "source_year": 2025,
            "source_page": 36,
        }
    ]
    # Opening the parent again must not re-run the extraction.
    app_client.get("/api/company/998812623")
    assert len(ai.pdfs) == 1


def test_non_parent_does_not_autostart(app_client, hjr, ai):
    assert (
        app_client.get("/api/company/998831067").json()["ownership"]["subsidiaries"]["status"]
        == "none_yet"
    )
    assert ai.pdfs == []


@pytest.mark.parametrize(
    "extraction,status",
    [
        (Extraction(note_found=False), "none"),
        (Extraction(note_found=True, rows=[SubsidiaryRow(name="  ")]), "none"),
    ],
    ids=["no-note", "blank-rows"],
)
def test_manual_extraction_without_note(app_client, hjr, ai, extraction, status):
    ai.extraction = extraction
    started = app_client.post("/api/company/998831067/subsidiaries")
    assert started.status_code == 202
    assert _wait(app_client, "998831067")["status"] == status


def test_extraction_failure_and_missing_key_are_reported(app_client, hjr, monkeypatch):
    from src import subsidiaries
    from src.main import app

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app.state.extractor = subsidiaries.gemini_extract
    app_client.post("/api/company/998831067/subsidiaries")
    assert (
        _wait(app_client, "998831067")["error"]
        == "AI er ikke konfigurert (mangler GEMINI_API_KEY)."
    )

    async def boom(pdf: bytes) -> Extraction:
        raise ValueError("bad json")

    app.state.extractor = boom
    app_client.post("/api/company/998831067/subsidiaries")
    failed = _wait(app_client, "998831067")
    assert (failed["status"], failed["error"]) == (
        "failed",
        "Kunne ikke lese årsrapporten. Prøv igjen senere.",
    )


def test_post_is_idempotent_unless_forced(app_client, hjr, ai):
    app_client.post("/api/company/998831067/subsidiaries")
    _wait(app_client, "998831067")
    app_client.post("/api/company/998831067/subsidiaries")
    assert len(ai.pdfs) == 1
    app_client.post("/api/company/998831067/subsidiaries?force=1")
    _wait(app_client, "998831067")
    assert len(ai.pdfs) == 2


BJONNASEN = "LÆRINGSVERKSTEDET BJØNNÅSEN SOLKOLLEN BARNEHAGE AS"
GARBLED = "Læringsverkstedet Bjørnåsen Solkollen Barnehage AS"


@pytest.mark.parametrize(
    "printed,expected",
    [
        ("830861262", ("830861262", BJONNASEN)),
        ("830 861 262", ("830861262", BJONNASEN)),
        ("830861263", (None, GARBLED)),  # wrong check digit → name lookup, which misses
        ("83086126", (None, GARBLED)),
        ("998812623", ("998812623", "HJR HOLDING AS")),  # checksum-valid: Brreg decides
        ("910000004", (None, GARBLED)),  # checksum-valid but unknown to Brreg → misread
        (None, (None, GARBLED)),
    ],
    ids=["valid", "spaced", "bad-checksum", "8-digits", "other-company", "unknown", "absent"],
)
def test_printed_orgnr_wins_and_brreg_name_shown(app_client, hjr, ai, printed, expected):
    """Læringsverkstedet 2025 (scanned): «Bjønnåsen» read as «Bjørnåsen», orgnr printed beside it."""
    hjr.company("830861262", BJONNASEN, [])
    ai.extraction = Extraction(note_found=True, rows=[SubsidiaryRow(name=GARBLED, orgnr=printed)])
    app_client.post("/api/company/998831067/subsidiaries")
    rows = _wait(app_client, "998831067")["rows"]
    assert [(r["orgnr"], r["name"]) for r in rows] == [expected]


def test_unknown_printed_orgnr_falls_back_to_name_match(app_client, hjr, ai):
    ai.extraction = Extraction(
        note_found=True, rows=[SubsidiaryRow(name="Dibber AS", orgnr="910000004")]
    )
    app_client.post("/api/company/998812623/subsidiaries")
    rows = _wait(app_client, "998812623")["rows"]
    assert [(r["orgnr"], r["name"]) for r in rows] == [("998831067", "DIBBER AS")]


def test_printed_orgnr_used_for_foreign_looking_rows_too(app_client, hjr, ai):
    ai.extraction = Extraction(
        note_found=True,
        rows=[SubsidiaryRow(name="Dibber AS", orgnr="998831067", country="Sverige")],
    )
    app_client.post("/api/company/998812623/subsidiaries")
    assert [r["orgnr"] for r in _wait(app_client, "998812623")["rows"]] == ["998831067"]


def test_prompt_excludes_merged_companies():
    """Læringsverkstedet 2025 listed «Innfusjonerte selskaper» (100 %) next to the real note."""
    from src.subsidiaries import PROMPT

    assert "innfusjonerte" in PROMPT.lower()
    assert "orgnr" in PROMPT


def test_post_without_pdfs_is_404(app_client, brreg):
    brreg.company("998812623", "HJR HOLDING AS", [])
    assert app_client.post("/api/company/998812623/subsidiaries").status_code == 404


def test_chat_context_includes_company_and_lists(app_client, hjr, ai):
    app_client.put("/api/favourites/998831067")
    resp = app_client.post(
        "/api/chat",
        json={
            "orgnr": "998812623",
            "messages": [{"role": "user", "content": "Hva er årsresultatet?"}],
        },
    )
    system = ai.systems[0]
    assert resp.json() == {"text": "Svar fra test."}
    assert all(
        s in system
        for s in (
            "HJR HOLDING AS",
            "aarsresultat: 25 264 000",
            "Styrets leder: Kari Nordmann",
            "favoritter: DIBBER AS",
        )
    )


def test_chat_context_includes_older_years(app_client, hjr, ai):
    hjr.figures["998812623"] = json.loads((FIXTURES / "hjr_regnskap_2023_2025.json").read_text())
    app_client.post(
        "/api/chat",
        json={"orgnr": "998812623", "messages": [{"role": "user", "content": "Utvikling?"}]},
    )
    system = ai.systems[0]
    assert (
        "Selskapsregnskap 2025 (2025-01-01" in system,
        "Selskapsregnskap 2023: driftsinntekter" in system,
        "Konsernregnskap 2025 (NOK): driftsinntekter" in system,
    ) == (True, True, True)


def test_chat_context_includes_open_collection(app_client, hjr, ai):
    cid = app_client.post("/api/collections", json={"name": "Barnehager"}).json()["id"]
    app_client.put(f"/api/collections/{cid}/members/998831067")
    app_client.post(
        "/api/chat",
        json={"collection_id": cid, "messages": [{"role": "user", "content": "Hvem er med?"}]},
    )
    assert "samlingen «Barnehager»: DIBBER AS" in ai.systems[0]


@pytest.mark.parametrize(
    "messages,status",
    [
        ([], 422),
        ([{"role": "assistant", "content": "hei"}], 400),
        ([{"role": "system", "content": "hei"}], 422),
        ([{"role": "user", "content": ""}], 422),
    ],
    ids=["empty", "only-assistant", "bad-role", "empty-content"],
)
def test_chat_rejects_bad_messages(app_client, messages, status):
    assert app_client.post("/api/chat", json={"messages": messages}).status_code == status


def test_chat_without_key_is_503(app_client, monkeypatch):
    from src.chat import gemini_chat
    from src.main import app

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app.state.chat_model = gemini_chat
    resp = app_client.post("/api/chat", json={"messages": [{"role": "user", "content": "hei"}]})
    assert (resp.status_code, resp.json()["detail"]) == (
        503,
        "AI er ikke konfigurert (mangler GEMINI_API_KEY).",
    )

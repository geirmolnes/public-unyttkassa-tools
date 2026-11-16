"""Smoke test against the real Brreg API. Skip with `-m "not live"`."""

import os

import httpx
import pytest

from src import db, subsidiaries
from src.brreg import BrregClient
from src.subsidiaries import gemini_extract, run_extraction

pytestmark = pytest.mark.live


async def test_live_hjr_holding():
    async with httpx.AsyncClient(timeout=60) as http:
        client = BrregClient(http)
        results = await client.search("hjr holding")
        years = await client.list_years("998812623")
        pdf = await client.fetch_pdf("998812623", 2024)
    assert results[0].orgnr == "998812623"
    assert {2012, 2024} <= set(years)
    assert pdf.startswith(b"%PDF")


@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="needs GEMINI_API_KEY")
async def test_live_subsidiaries_hjr():
    """Real Gemini read of HJR's 2025 report: note 23 lists Dibber AS (998831067) among many rows."""
    async with httpx.AsyncClient(timeout=120) as http:
        client = BrregClient(http)
        db.start_extraction("998812623", 2025, subsidiaries.MODEL)
        await run_extraction(client, "998812623", 2025, gemini_extract)
    status = subsidiaries.status("998812623")
    by_name = {r["name"].upper(): r for r in status["rows"]}
    print(
        f"\nstatus={status['status']} error={status['error']} rows={len(status['rows'])} "
        f"resolved={sum(1 for r in status['rows'] if r['orgnr'])}"
    )
    assert status["status"] == "ok", status
    assert len(status["rows"]) > 100
    assert by_name["DIBBER AS"]["orgnr"] == "998831067"


@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="needs GEMINI_API_KEY")
async def test_live_subsidiaries_laeringsverkstedet():
    """Scanned 2025 report: note 7 lists 7 merged-in companies (100 %), note 13 the ~250 real
    subsidiaries with printed orgnr and no share. Only note 13 belongs, orgnr from the PDF,
    names from Brreg."""
    async with httpx.AsyncClient(timeout=180) as http:
        client = BrregClient(http)
        db.start_extraction("878620852", 2025, subsidiaries.MODEL)
        await run_extraction(client, "878620852", 2025, gemini_extract)
    status = subsidiaries.status("878620852")
    rows = status["rows"]
    by_name = {r["name"]: r for r in rows}
    resolved = sum(1 for r in rows if r["orgnr"])
    print(f"\nstatus={status['status']} rows={len(rows)} resolved={resolved}")
    assert status["status"] == "ok", status
    assert not any("Eplehagen" in n or "Sundbakken" in n for n in by_name)
    assert len(rows) > 200 and resolved / len(rows) > 0.95
    assert by_name["LÆRINGSVERKSTEDET ALVERSUND BARNEHAGE AS"]["orgnr"] == "930861642"
    # Scan reads «Munbekekken»/«Bjørnåsen»; linked rows carry Brreg's registered name instead.
    assert {
        "LÆRINGSVERKSTEDET MUNKEBEKKEN IDRETTSBARNEHAGE AS",
        "LÆRINGSVERKSTEDET BJØNNÅSEN SOLKOLLEN BARNEHAGE AS",
    } <= set(by_name)

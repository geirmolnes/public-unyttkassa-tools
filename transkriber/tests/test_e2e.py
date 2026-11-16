"""End-to-end test against a real ElevenLabs Scribe transcription call.

Runs only when `ELEVENLABS_API_KEY` is set in the environment. Uses a ~22
second Norwegian speech sample checked in at tests/fixtures/test_long.mp3 so
the test is repeatable and offline-friendly up to the Scribe call itself.

Skip locally without credentials; run manually with the key in the environment:

  ELEVENLABS_API_KEY=<key> uv run pytest tests/test_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from .conftest import TEST_EMAIL, _signed_cookie

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "test_long.mp3"

# Capture the real key at import time — ELEVENLABS_API_KEY is read per-call
# in transcribe_file, but conftest's autouse env fixture runs before each
# test, so we re-export it explicitly inside the test.
REAL_ELEVENLABS_KEY = os.environ.get("ELEVENLABS_API_KEY")

pytestmark = [
    pytest.mark.skipif(
        not REAL_ELEVENLABS_KEY,
        reason="ELEVENLABS_API_KEY not set in shell — skipping live transcription test",
    ),
    pytest.mark.skipif(
        not FIXTURE_PATH.exists(),
        reason=f"Fixture audio missing at {FIXTURE_PATH}",
    ),
]


@pytest.mark.asyncio
async def test_full_upload_poll_view_cycle(monkeypatch):
    """Upload → poll /status → GET /t/{id} → assert real transcript embedded.

    Uses httpx AsyncClient + ASGITransport so the background transcription
    task created via asyncio.create_task actually runs to completion —
    TestClient's synchronous wrapper drops those tasks when a request ends.

    The Gemini post-passes (questions/cleanup) run with the placeholder key
    from conftest and fail soft in the background; they're not asserted here.
    """
    monkeypatch.setenv("ELEVENLABS_API_KEY", REAL_ELEVENLABS_KEY)

    from httpx import ASGITransport, AsyncClient

    import src.main as main_module

    headers = {"Cookie": f"session={_signed_cookie(TEST_EMAIL)}"}
    transport = ASGITransport(app=main_module.app)

    async with AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers,
    ) as client:
        # 1. Upload the fixture.
        with FIXTURE_PATH.open("rb") as f:
            resp = await client.post(
                "/upload",
                files={"file": (FIXTURE_PATH.name, f, "audio/mpeg")},
            )
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]

        # 2. Poll /status until done (or error).
        deadline = asyncio.get_event_loop().time() + 180
        status = None
        while asyncio.get_event_loop().time() < deadline:
            status = (await client.get(f"/status/{job_id}")).json()
            if status["status"] == "done":
                break
            if status["status"] == "error":
                pytest.fail(f"Transcription failed: {status.get('error')}")
            await asyncio.sleep(3)
        else:
            pytest.fail(f"Transcription did not finish within 180s; last status={status}")

        # Plain polls are lightweight by design; the body needs ?full=1.
        full = (await client.get(f"/status/{job_id}", params={"full": 1})).json()
        transcript = full["transcript"]
        assert transcript["language"] in ("no", "no+en", "en")
        assert len(transcript["segments"]) > 0
        # The fixture mentions "Norge" — fuzzy substring check that proves real
        # audio actually went through Scribe rather than a stub response.
        joined = " ".join(s["text"] for s in transcript["segments"]).lower()
        assert "norge" in joined or "norway" in joined, f"Expected 'Norge' in transcript: {joined}"

        # 3. Fetch the canonical transcript page and assert the JSON is embedded
        #    correctly in the HTML for the client-side JS to pick up.
        page = await client.get(f"/t/{job_id}")
        assert page.status_code == 200
        assert "data-transcript=" in page.text
        assert FIXTURE_PATH.name in page.text

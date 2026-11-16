"""Integration tests for the HTTP API + template rendering.

Uses the real FastAPI app. We bypass the Gemini call by inserting a completed
transcript row directly into SQLite — the goal here is to catch regressions in
routing, auth, and *especially* how the transcript is embedded in the HTML,
since that's what broke before.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import TEST_EMAIL, _signed_cookie


def _insert_done_transcript(job_id: str = "abc123"):
    """Seed the DB with a finished transcript. Returns the expected payload."""
    import src.db as db
    from src.transcribe import Segment, Transcript

    db.create_job(job_id, TEST_EMAIL, "interview.mp3", "/tmp/fake.mp3")
    transcript = Transcript(
        language="no",
        segments=[
            Segment(
                start_seconds=0.0,
                end_seconds=5.0,
                text="Kan du fortelle om prosjektet?",
                is_question=True,
            ),
            Segment(
                start_seconds=5.2,
                end_seconds=9.5,
                text="Ja, det startet i 2008.",
                is_question=False,
            ),
        ],
    )
    db.save_result(job_id, transcript)
    return transcript


def test_health_is_public_no_auth_required():
    """Regression: healthz must stay public so Dokku's liveness probe works."""
    from fastapi.testclient import TestClient

    import src.main as main_module

    resp = TestClient(main_module.app).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_unauthenticated_requests_redirect_to_login():
    from fastapi.testclient import TestClient

    import src.main as main_module

    resp = TestClient(main_module.app).get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.org/login"


@pytest.mark.parametrize(
    "host",
    [
        "testserver",
        "testserver/healthz?",
        "testserver/healthz#",
        "testserver/api/stats?",
        "testserver/api/stats#",
    ],
    ids=[
        "plain-host",
        "smuggles-healthz-query",
        "smuggles-healthz-fragment",
        "smuggles-stats-prefix-query",
        "smuggles-stats-prefix-fragment",
    ],
)
def test_poisoned_host_cannot_borrow_public_path_exemption(host):
    """A Host header that puts a public path into request.url.path must not skip
    auth (CVE-2026-48710). Server exceptions are returned as 500s so a bypass
    fails the assertion instead of erroring on the missing user."""
    from fastapi.testclient import TestClient

    import src.main as main_module

    resp = TestClient(main_module.app, raise_server_exceptions=False).get(
        "/", headers={"host": host}, follow_redirects=False
    )
    assert (resp.status_code, resp.headers.get("location")) == (302, "https://example.org/login")


# A signed token is not a session unless it carries an email.
@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"user_id": 1, "email": "a@x.no"}, 200),
        ({"user_id": 1, "email": "a"}, 200),
        ({"user_id": 1, "email": ""}, 302),
        ({"user_id": 1}, 302),
        ({"purpose": "login", "uid": 1}, 302),
        ("a@x.no", 302),
        (None, 302),
    ],
    ids=[
        "hub-session",
        "one-char-email",
        "empty-email",
        "no-email",
        "magic-link-token",
        "bare-string",
        "null",
    ],
)
def test_only_a_signed_session_with_an_email_gets_in(payload, status):
    from fastapi.testclient import TestClient

    import src.main as main_module

    from .conftest import signed_body

    token = signed_body(payload)
    resp = TestClient(main_module.app, raise_server_exceptions=False).get(
        "/", headers={"cookie": f"session={token}"}, follow_redirects=False
    )
    assert resp.status_code == status


def test_index_page_renders_for_authenticated_user(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Ny transkripsjon" in resp.text
    # Authenticated chrome is the logout control, NOT the email: 83ae47c
    # deliberately hid the email from the UI (auth still uses it internally).
    assert "Logg ut" in resp.text
    assert TEST_EMAIL not in resp.text


def test_transcript_page_embeds_parseable_json(client):
    """THE regression test: the transcript page must embed the segments as
    valid JSON in data-transcript so that the client-side JS can parse it.
    """
    expected = _insert_done_transcript("xyz789")

    resp = client.get("/t/xyz789")
    assert resp.status_code == 200
    assert "interview.mp3" in resp.text

    # Extract the data-transcript attribute.
    match = re.search(r"data-transcript='([^']+)'", resp.text)
    assert match, "data-transcript attribute not found in rendered HTML"

    # Jinja's tojson filter escapes unicode-safe for HTML; JSON.parse handles it.
    payload = json.loads(match.group(1))
    assert payload["language"] == "no"
    assert len(payload["segments"]) == len(expected.segments)
    assert payload["segments"][0]["text"] == "Kan du fortelle om prosjektet?"
    assert payload["segments"][0]["is_question"] is True
    assert payload["segments"][1]["is_question"] is False


def test_transcript_page_returns_404_for_other_users_transcript(client):
    """A user must not see another user's transcript even if they guess the ID."""
    import src.db as db

    db.create_job("other-user-job", "stranger@example.com", "secret.mp3", "/tmp/x.mp3")

    resp = client.get("/t/other-user-job")
    assert resp.status_code == 404


def test_delete_removes_transcript(client):
    _insert_done_transcript("to-delete")

    resp = client.post("/t/to-delete/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    # Follow-up GET should 404.
    assert client.get("/t/to-delete").status_code == 404


def test_audio_serves_file(client, tmp_path):
    import src.db as db

    audio = tmp_path / "interview.mp3"
    audio.write_bytes(b"fake-mp3-bytes")
    db.create_job("with-audio", TEST_EMAIL, "interview.mp3", str(audio))

    resp = client.get("/audio/with-audio")
    assert resp.status_code == 200
    assert resp.content == b"fake-mp3-bytes"


def test_audio_missing_file_returns_404_not_500(client, tmp_path):
    """Regression guard: a purged/lost audio file must 404 with a friendly
    message, not crash FileResponse into a 500."""
    import src.db as db

    db.create_job("no-file", TEST_EMAIL, "x.mp3", str(tmp_path / "gone.mp3"))

    resp = client.get("/audio/no-file")
    assert resp.status_code == 404


def test_audio_other_users_job_returns_404(client, tmp_path):
    import src.db as db

    audio = tmp_path / "theirs.mp3"
    audio.write_bytes(b"x")
    db.create_job("their-audio", "stranger@example.com", "theirs.mp3", str(audio))

    assert client.get("/audio/their-audio").status_code == 404


@pytest.mark.parametrize("ext", [".wav", ".mp3", ".m4a", ".ogg"])
def test_upload_accepts_supported_formats(client, ext):
    resp = client.post(
        "/upload",
        files={"file": (f"clip{ext}", b"fake-audio-bytes", "application/octet-stream")},
    )
    # The job is queued and transcription is dispatched to a background task.
    # We don't wait for Gemini here; we just verify the upload path accepts
    # the file and creates a row.
    assert resp.status_code == 200
    assert "job_id" in resp.json()


def test_upload_rejects_unsupported_format(client):
    resp = client.post(
        "/upload",
        files={"file": ("doc.txt", b"not audio", "text/plain")},
    )
    assert resp.status_code == 400
    assert "Ustøttet" in resp.json()["detail"]


def test_upload_stamps_provider_on_row(client):
    """Every row is stamped 'elevenlabs' at creation so the admin dashboard
    distinguishes new rows from legacy Gemini-transcribed ones."""
    import src.db as db

    resp = client.post(
        "/upload",
        files={"file": ("clip.mp3", b"fake-audio", "audio/mpeg")},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert db.get(job_id, TEST_EMAIL)["provider"] == "elevenlabs"


def test_save_result_then_save_questions_flow(client):
    """save_result leaves questions_done=0 so the UI polls; save_questions
    flips it to 1 and writes the is_question flags in."""
    import src.db as db
    from src.transcribe import Segment, Transcript

    job_id = "twostep"
    db.create_job(job_id, TEST_EMAIL, "x.mp3", "/tmp/x.mp3")

    plain = Transcript(
        language="no",
        segments=[
            Segment(start_seconds=0, end_seconds=2, text="Hva heter du?"),
            Segment(start_seconds=2, end_seconds=5, text="Kari."),
        ],
    )
    db.save_result(job_id, plain)

    # After save_result: status is done but questions aren't flagged yet.
    # Plain polls stay lightweight — no transcript body without ?full=1.
    resp = client.get(f"/status/{job_id}").json()
    assert resp["status"] == "done"
    assert resp["questions_done"] is False
    assert "transcript" not in resp

    full = client.get(f"/status/{job_id}", params={"full": 1}).json()
    assert all(s["is_question"] is False for s in full["transcript"]["segments"])

    # Second pass patches in the flags and flips questions_done.
    flagged = Transcript(
        language="no",
        segments=[
            Segment(start_seconds=0, end_seconds=2, text="Hva heter du?", is_question=True),
            Segment(start_seconds=2, end_seconds=5, text="Kari.", is_question=False),
        ],
    )
    db.save_questions(job_id, flagged)

    resp = client.get(f"/status/{job_id}", params={"full": 1}).json()
    assert resp["questions_done"] is True
    assert resp["transcript"]["segments"][0]["is_question"] is True
    assert resp["transcript"]["segments"][1]["is_question"] is False


def test_mark_questions_done_without_flags_on_failure(client):
    """If the question-detection pass fails, we still flip questions_done
    so the UI stops polling (transcript just shows without italics)."""
    import src.db as db
    from src.transcribe import Segment, Transcript

    db.create_job("failjob", TEST_EMAIL, "x.mp3", "/tmp/x.mp3")
    db.save_result(
        "failjob",
        Transcript(language="no", segments=[Segment(start_seconds=0, end_seconds=1, text="hi")]),
    )

    # Simulate question-pass failure path.
    db.mark_questions_done("failjob")

    resp = client.get("/status/failjob").json()
    assert resp["questions_done"] is True


async def test_interrupted_jobs_are_requeued_on_startup(client, tmp_path, monkeypatch):
    """On container restart, in-flight jobs whose audio still exists are
    re-dispatched instead of failed — the user never sees the interruption."""
    import asyncio

    import src.db as db
    import src.main as main_module

    audio = tmp_path / "running.mp3"
    audio.write_bytes(b"x")
    db.create_job("r1", TEST_EMAIL, "running.mp3", str(audio))
    db.mark_running("r1")
    # A completed job must NOT be touched.
    db.create_job("d1", TEST_EMAIL, "done.mp3", "/tmp/x.mp3")
    from src.transcribe import Segment, Transcript

    db.save_result(
        "d1",
        Transcript(language="no", segments=[Segment(start_seconds=0, end_seconds=1, text="hi")]),
    )

    dispatched = []

    async def fake_run_job(job_id, audio_path):
        dispatched.append(job_id)

    monkeypatch.setattr(main_module, "_run_job", fake_run_job)
    main_module._requeue_interrupted_jobs()
    await asyncio.sleep(0)  # let the spawned stub task run

    assert dispatched == ["r1"]
    assert db.get("r1", TEST_EMAIL)["status"] == "queued"
    assert db.get("r1", TEST_EMAIL)["requeue_count"] == 1
    assert db.get("d1", TEST_EMAIL)["status"] == "done"


async def test_interrupted_jobs_fail_when_budget_spent_or_audio_gone(client, tmp_path, monkeypatch):
    import src.db as db
    import src.main as main_module

    # Audio file is gone — can't requeue.
    db.create_job("gone", TEST_EMAIL, "gone.mp3", str(tmp_path / "deleted.mp3"))
    db.mark_running("gone")

    # Retry budget exhausted — must not crash-loop.
    audio = tmp_path / "loop.mp3"
    audio.write_bytes(b"x")
    db.create_job("loopy", TEST_EMAIL, "loop.mp3", str(audio))
    db.mark_running("loopy")
    for _ in range(main_module.MAX_REQUEUES):
        db.requeue_job("loopy")

    dispatched = []

    async def fake_run_job(job_id, audio_path):
        dispatched.append(job_id)

    monkeypatch.setattr(main_module, "_run_job", fake_run_job)
    main_module._requeue_interrupted_jobs()

    assert dispatched == []
    for job_id in ("gone", "loopy"):
        row = db.get(job_id, TEST_EMAIL)
        assert row["status"] == "error"
        # Post-pass flags flip too, so the UI never polls a dead job forever.
        assert row["questions_done"] == 1
        assert row["cleaned_done"] == 1


def test_signed_cookie_round_trips():
    """Sanity: our test fixture signs cookies the way the app verifies them."""
    from src.session_token import verify_session

    from .conftest import TEST_EMAIL

    payload = verify_session(_signed_cookie(TEST_EMAIL))
    assert payload["email"] == TEST_EMAIL


# ------------------------------------------------------------------------------
# Upload size caps. The middleware rejects from Content-Length before the body
# is parsed; the in-endpoint streaming loop backstops requests that lie.
# ------------------------------------------------------------------------------


def test_upload_rejected_by_content_length_middleware(client, monkeypatch):
    import src.main as main_module

    monkeypatch.setattr(main_module, "MAX_UPLOAD_BYTES", 10)
    # 2 MB body: declared Content-Length far exceeds cap + 1 MB slack.
    resp = client.post(
        "/upload",
        files={"file": ("big.mp3", b"x" * (2 * 1024 * 1024), "audio/mpeg")},
    )
    assert resp.status_code == 413
    assert "større enn" in resp.json()["detail"]


@pytest.mark.parametrize(
    "host",
    ["testserver", "testserver/elsewhere?", "testserver/elsewhere#"],
    ids=["plain-host", "poisoned-query", "poisoned-fragment"],
)
def test_poisoned_host_cannot_dodge_the_upload_size_gate(monkeypatch, host):
    """The size gate sits outside auth, so an anonymous oversized upload gets 413 from
    it; a 302 to login would mean a poisoned Host moved request.url.path off /upload."""
    from fastapi.testclient import TestClient

    import src.main as main_module

    monkeypatch.setattr(main_module, "MAX_UPLOAD_BYTES", 10)
    resp = TestClient(main_module.app, raise_server_exceptions=False).post(
        "/upload",
        headers={"host": host},
        files={"file": ("big.mp3", b"x" * (2 * 1024 * 1024), "audio/mpeg")},
        follow_redirects=False,
    )
    assert resp.status_code == 413


def test_upload_streaming_cap_backstop(client, monkeypatch):
    """A small over-cap body slips past the middleware's 1 MB slack but must
    still be caught (and cleaned up) by the streaming loop."""
    import src.main as main_module

    monkeypatch.setattr(main_module, "MAX_UPLOAD_BYTES", 10)
    resp = client.post(
        "/upload",
        files={"file": ("small.mp3", b"x" * 20, "audio/mpeg")},
    )
    assert resp.status_code == 413
    # The partial file must not linger on disk.
    leftovers = [p for p in main_module.UPLOAD_DIR.iterdir() if p.suffix == ".mp3"]
    assert leftovers == []


# ------------------------------------------------------------------------------
# Export.
# ------------------------------------------------------------------------------


def _insert_cleanable_transcript(job_id: str = "exportjob"):
    import src.db as db
    from src.transcribe import Segment, Transcript

    db.create_job(job_id, TEST_EMAIL, "intervju.mp3", "/tmp/fake.mp3")
    transcript = Transcript(
        language="no",
        segments=[
            Segment(
                start_seconds=0.0,
                end_seconds=5.0,
                text="– Eh, kan du fortelle om prosjektet?",
                cleaned_text="– Kan du fortelle om prosjektet?",
                is_question=True,
            ),
            Segment(
                start_seconds=5.2,
                end_seconds=9.5,
                text="– Ja, det startet i 2008.",
                is_question=False,
            ),
        ],
    )
    db.save_result(job_id, transcript)
    return transcript


def test_export_txt_uses_cleaned_mode_by_default(client):
    _insert_cleanable_transcript()

    resp = client.get("/t/exportjob/export", params={"format": "txt"})
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert "intervju.txt" in resp.headers["content-disposition"]
    body = resp.text
    assert "– Kan du fortelle om prosjektet?" in body  # cleaned variant
    assert "Eh," not in body
    assert "– Ja, det startet i 2008." in body


def test_export_txt_raw_mode(client):
    _insert_cleanable_transcript()

    resp = client.get("/t/exportjob/export", params={"format": "txt", "mode": "raw"})
    assert "– Eh, kan du fortelle om prosjektet?" in resp.text


def test_export_srt_has_timestamps_and_indices(client):
    _insert_cleanable_transcript()

    resp = client.get("/t/exportjob/export", params={"format": "srt"})
    assert resp.status_code == 200
    assert "1\n00:00:00,000 --> 00:00:05,000" in resp.text
    assert "2\n00:00:05,200 --> 00:00:09,500" in resp.text


def test_export_vtt_has_header_and_dot_timestamps(client):
    _insert_cleanable_transcript()

    resp = client.get("/t/exportjob/export", params={"format": "vtt"})
    assert resp.status_code == 200
    assert resp.text.startswith("WEBVTT\n")
    assert "00:00:00.000 --> 00:00:05.000" in resp.text


def test_export_docx_returns_word_document(client):
    _insert_cleanable_transcript()

    resp = client.get("/t/exportjob/export", params={"format": "docx"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument")
    assert resp.content[:2] == b"PK"  # docx is a zip container


def test_export_unknown_format_rejected(client):
    _insert_cleanable_transcript()

    assert client.get("/t/exportjob/export", params={"format": "pdf"}).status_code == 400


def test_export_other_users_transcript_404(client):
    import src.db as db

    db.create_job("not-mine", "stranger@example.com", "x.mp3", "/tmp/x.mp3")
    assert client.get("/t/not-mine/export", params={"format": "txt"}).status_code == 404


# ------------------------------------------------------------------------------
# Speaker renaming.
# ------------------------------------------------------------------------------


def test_rename_speaker_round_trips(client):
    import src.db as db

    _insert_done_transcript("spk")

    resp = client.post("/t/spk/speakers", json={"speaker_id": "speaker_0", "name": "Kari Nordmann"})
    assert resp.status_code == 200
    assert db.get("spk", TEST_EMAIL)["speaker_names"] == {"speaker_0": "Kari Nordmann"}

    # Empty name clears the custom label again.
    resp = client.post("/t/spk/speakers", json={"speaker_id": "speaker_0", "name": ""})
    assert resp.status_code == 200
    assert db.get("spk", TEST_EMAIL)["speaker_names"] == {}


def test_rename_speaker_other_users_transcript_404(client):
    import src.db as db

    db.create_job("their-spk", "stranger@example.com", "x.mp3", "/tmp/x.mp3")
    resp = client.post("/t/their-spk/speakers", json={"speaker_id": "speaker_0", "name": "X"})
    assert resp.status_code == 404


# ------------------------------------------------------------------------------
# /api/stats auth.
# ------------------------------------------------------------------------------


def test_stats_disabled_without_configured_key(client):
    resp = client.get("/api/stats")
    assert resp.status_code == 503


def test_stats_rejects_wrong_key(client, monkeypatch):
    import src.main as main_module

    monkeypatch.setattr(main_module, "_EXPECTED_API_KEY", "riktig-nokkel")
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", headers={"X-API-Key": "feil"}).status_code == 401


def test_stats_accepts_correct_key_and_reports_disk(client, monkeypatch):
    import src.main as main_module

    monkeypatch.setattr(main_module, "_EXPECTED_API_KEY", "riktig-nokkel")
    resp = client.get("/api/stats", headers={"X-API-Key": "riktig-nokkel"})
    assert resp.status_code == 200
    data = resp.json()
    assert "totals" in data
    assert "disk" in data
    assert set(data["disk"]) == {"audio_bytes", "audio_files"}


# ------------------------------------------------------------------------------
# Audio retention sweep.
# ------------------------------------------------------------------------------


def test_migration_strips_leaked_index_prefixes(client):
    """Backfill: rows cleaned before the write-time guard have a leaked "N: "
    index in cleaned_text. The user_version-gated migration strips them."""
    import json

    import src.db as db

    db.create_job("leaked", TEST_EMAIL, "x.mp3", "/tmp/x.mp3")
    segments = [
        {
            "start_seconds": 0.0,
            "end_seconds": 2.0,
            "text": "– Hei.",
            "is_question": False,
            "cleaned_text": "0: – Hei.",
            "speaker": None,
        },
        {
            "start_seconds": 2.0,
            "end_seconds": 4.0,
            "text": "– Det startet i 2008.",
            "is_question": False,
            "cleaned_text": "1: – Det startet i 2008.",
            "speaker": None,
        },
    ]
    with db._connect() as conn:
        conn.execute(
            "UPDATE transcripts SET status='done', segments_json=? WHERE id='leaked'",
            (json.dumps(segments),),
        )
        conn.execute("PRAGMA user_version = 0")  # pretend the migration hasn't run

    db.init_db()  # triggers the gated migration

    row = db.get("leaked", TEST_EMAIL)
    cleaned = [s["cleaned_text"] for s in row["transcript"]["segments"]]
    # Index prefixes gone; the legitimate "2008" inside the text is preserved.
    assert cleaned == ["– Hei.", "– Det startet i 2008."]


def test_retention_disabled_by_default(client, tmp_path):
    """AUDIO_RETENTION_DAYS defaults to 0 — the sweep must be a no-op until
    someone explicitly opts in via the env var."""
    import src.db as db
    import src.main as main_module

    audio = tmp_path / "ancient.mp3"
    audio.write_bytes(b"x")
    db.create_job("ancient", TEST_EMAIL, "ancient.mp3", str(audio))
    db.save_error("ancient", "x")
    with db._connect() as conn:
        conn.execute(
            "UPDATE transcripts SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = 'ancient'"
        )

    assert main_module.AUDIO_RETENTION_DAYS == 0
    assert main_module._purge_old_audio() == 0
    assert audio.exists()


def test_retention_purges_old_audio_but_keeps_transcript(client, tmp_path, monkeypatch):
    import src.db as db
    import src.main as main_module
    from src.transcribe import Segment, Transcript

    monkeypatch.setattr(main_module, "AUDIO_RETENTION_DAYS", 90)

    old_audio = tmp_path / "old.mp3"
    old_audio.write_bytes(b"old")
    db.create_job("oldjob", TEST_EMAIL, "old.mp3", str(old_audio))
    db.save_result(
        "oldjob",
        Transcript(language="no", segments=[Segment(start_seconds=0, end_seconds=1, text="hei")]),
    )
    with db._connect() as conn:
        conn.execute(
            "UPDATE transcripts SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = 'oldjob'"
        )

    fresh_audio = tmp_path / "fresh.mp3"
    fresh_audio.write_bytes(b"fresh")
    db.create_job("freshjob", TEST_EMAIL, "fresh.mp3", str(fresh_audio))
    db.save_result(
        "freshjob",
        Transcript(language="no", segments=[Segment(start_seconds=0, end_seconds=1, text="hei")]),
    )

    purged = main_module._purge_old_audio()
    assert purged == 1
    assert not old_audio.exists()
    assert fresh_audio.exists()

    row = db.get("oldjob", TEST_EMAIL)
    assert row["audio_purged"] == 1
    assert row["transcript"] is not None  # text survives

    # Purged audio: friendly 404, and the transcript page still renders.
    assert client.get("/audio/oldjob").status_code == 404
    page = client.get("/t/oldjob")
    assert page.status_code == 200
    assert "Lydfilen er slettet" in page.text


def test_retention_never_touches_running_jobs(client, tmp_path, monkeypatch):
    import src.db as db
    import src.main as main_module

    monkeypatch.setattr(main_module, "AUDIO_RETENTION_DAYS", 90)

    audio = tmp_path / "inflight.mp3"
    audio.write_bytes(b"x")
    db.create_job("inflight", TEST_EMAIL, "inflight.mp3", str(audio))
    db.mark_running("inflight")
    with db._connect() as conn:
        conn.execute(
            "UPDATE transcripts SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = 'inflight'"
        )

    assert main_module._purge_old_audio() == 0
    assert audio.exists()


# --- v2 (Ed25519) session tokens ---

V2_LOGIN = (302, "https://example.org/login")


def _index(cookie: str) -> tuple[int, str | None]:
    from fastapi.testclient import TestClient

    import src.main as main_module

    resp = TestClient(main_module.app).get(
        "/", headers={"cookie": f"session={cookie}"}, follow_redirects=False
    )
    return resp.status_code, resp.headers.get("location")


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, (200, None)),
        ({"signing_key": "other"}, V2_LOGIN),
        ({"issuer": "example.net"}, V2_LOGIN),
        ({"age": 61, "ttl": 60}, V2_LOGIN),
        ({"age": 55, "ttl": 60}, (200, None)),
    ],
    ids=["valid", "other-key", "other-fleet", "expired-1s-ago", "expires-in-5s"],
)
def test_v2_session_tokens(mint_v2, kwargs, expected):
    from src.session_token import generate_keypair

    if kwargs.get("signing_key") == "other":
        kwargs = {**kwargs, "signing_key": generate_keypair("ukd-other")[0]}
    assert _index(mint_v2({"user_id": 1, "email": "journalist@example.com"}, **kwargs)) == expected


def legacy_cookie(secret: str, claims: dict, now: int | None = None) -> str:
    """A cookie in the retired itsdangerous format (URLSafeTimedSerializer, default salt)."""

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = b64(json.dumps(claims, separators=(",", ":")).encode())
    at = int(time.time()) if now is None else now
    value = f"{payload}.{b64(at.to_bytes(4, 'big'))}"
    key = hashlib.sha1(b"itsdangerous" + b"signer" + secret.encode()).digest()
    return f"{value}.{b64(hmac.new(key, value.encode(), hashlib.sha1).digest())}"


# The pre-v2 cookie, correctly signed for SECRET_KEY="legacy-test-secret". Minted at test time: a
# fixed literal would age past the old 30-day max_age and then prove nothing.
OLD_COOKIE = legacy_cookie("legacy-test-secret", {"user_id": 7, "email": "kollega@example.no"})


@pytest.mark.parametrize("accept_legacy", ["1", "0"])
def test_an_old_format_cookie_is_refused(monkeypatch, accept_legacy):
    """Only v2 counts, whatever SESSION_ACCEPT_LEGACY and SECRET_KEY are left in the config."""
    import importlib

    import src.auth as auth_module
    import src.main as main_module
    from src.session_token import session_settings

    monkeypatch.setenv("SESSION_ACCEPT_LEGACY", accept_legacy)
    monkeypatch.setenv("SECRET_KEY", "legacy-test-secret")
    session_settings.cache_clear()
    importlib.reload(auth_module)
    importlib.reload(main_module)
    assert _index(OLD_COOKIE) == V2_LOGIN


def test_a_login_works_with_SECRET_KEY_set(mint_v2, monkeypatch):
    import importlib

    import src.auth as auth_module
    import src.main as main_module

    monkeypatch.setenv("SECRET_KEY", "legacy-test-secret")
    importlib.reload(auth_module)
    importlib.reload(main_module)
    assert _index(mint_v2({"user_id": 1, "email": "journalist@example.com"})) == (200, None)


ANON_PROBE = (
    "from fastapi.testclient import TestClient\n"
    "r = TestClient(src.main.app).get('/', follow_redirects=False)\n"
    "assert (r.status_code, r.headers.get('location')) == "
    "(302, 'https://example.org/login'), r.status_code\n"
)


def _import_in_prod(tmp_path, extra: dict[str, str]) -> subprocess.CompletedProcess:
    """Import src.main in a fresh interpreter with a Dokku-like env and no SECRET_KEY."""
    from .conftest import TEST_ISSUER, TEST_PUBLIC_KEY

    drop = ("SECRET_KEY", "DEV_INSECURE_AUTH", "SESSION_ACCEPT_LEGACY", "DOKKU_APP_NAME")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env |= {
        "DOKKU_APP_TYPE": "dockerfile",
        "SESSION_PUBLIC_KEYS": TEST_PUBLIC_KEY,
        "SESSION_ISSUER": TEST_ISSUER,
        "UPLOAD_DIR": str(tmp_path),
        "TRANSKRIBER_DB": str(tmp_path / "transkriber.db"),
        **extra,
    }
    return subprocess.run(
        [sys.executable, "-c", f"import src.main\n{ANON_PROBE}"],
        check=False,
        cwd=Path(__file__).parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "extra",
    [{}, {"SECRET_KEY": "legacy-test-secret"}, {"DEV_INSECURE_AUTH": "1"}],
    ids=["no-secret", "secret-set", "old-dev-flag-ignored"],
)
def test_the_app_starts_in_prod_and_gates_anonymous_requests(tmp_path, extra):
    result = _import_in_prod(tmp_path, extra)
    assert result.returncode == 0, result.stderr


def test_v2_session_scopes_jobs_to_its_email(mint_v2):
    """Jobs are per journalist: the v2 email must reach get_user_email(), lowercased."""
    from fastapi import Depends
    from fastapi.testclient import TestClient

    import src.main as main_module
    from src.auth import get_user_email

    @main_module.app.get("/__whoami")
    def whoami(email: str = Depends(get_user_email)):
        return {"email": email}

    token = mint_v2({"user_id": 1, "email": "Journalist@Example.com"})
    resp = TestClient(main_module.app).get("/__whoami", headers={"cookie": f"session={token}"})
    assert resp.json() == {"email": "journalist@example.com"}


def test_legacy_cookie_matches_a_real_itsdangerous_token():
    """At a fixed time the helper reproduces, byte for byte, a token itsdangerous 2.2 minted."""
    claims = {"user_id": 7, "email": "kollega@example.no"}
    assert legacy_cookie("legacy-test-secret", claims, now=1_794_219_054) == (
        "eyJ1c2VyX2lkIjo3LCJlbWFpbCI6ImtvbGxlZ2FAZXhhbXBsZS5ubyJ9.avGcLg.gVn_UWNcyzacHDFq1COCQZWggZA"
    )


@pytest.mark.parametrize(
    ("env", "login", "logout"),
    [
        ({}, "https://example.org/login", "https://example.org/logout"),
        ({"LOGIN_URL": "/inn", "LOGOUT_URL": "/ut"}, "/inn", "/ut"),
    ],
    ids=["default", "overridden"],
)
def test_login_and_logout_urls_come_from_the_environment(monkeypatch, env, login, logout):
    import importlib

    from fastapi.testclient import TestClient

    import src.auth as auth_module
    import src.main as main_module

    for name in ("LOGIN_URL", "LOGOUT_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    importlib.reload(auth_module)
    importlib.reload(main_module)

    assert _index("") == (302, login)
    page = TestClient(main_module.app).get(
        "/", headers={"cookie": f"session={_signed_cookie(TEST_EMAIL)}"}
    )
    assert f'<form action="{logout}" method="post"' in page.text

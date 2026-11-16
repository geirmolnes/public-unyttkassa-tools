"""Shared fixtures. Brreg is mocked with respx; a throwaway Ed25519 key lets us mint session cookies."""

import base64
import json
import os
import re
import time

os.environ.pop("SECRET_KEY", None)
os.environ.pop("DEV_INSECURE_AUTH", None)
os.environ["ACCOUNTS_WATCH_S"] = "0"
os.environ.pop("AI_USAGE_TOKEN", None)  # tests never report usage unless they opt in
os.environ.pop("LOGIN_URL", None)  # tests expect the built-in addresses unless they set their own
os.environ.pop("LOGOUT_URL", None)
os.environ["REGNSKAP_ADMINS"] = (
    "admin@example.com"  # tests resume stale reads by calling pipeline.resume_stale
)

from src.session_token import generate_keypair, parse_signing_key, sign_v2  # noqa: E402

# Login signing: a throwaway Ed25519 key.
TEST_SIGNING_KEY, TEST_PUBLIC_KEY = generate_keypair("ukd-test")
TEST_ISSUER = "example.org"
os.environ.update(SESSION_PUBLIC_KEYS=TEST_PUBLIC_KEY, SESSION_ISSUER=TEST_ISSUER)

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

from src import db, jobs, limits, subsidiaries  # noqa: E402
from src.accounts.llm import NO_KEY, MissingKey, Usage  # noqa: E402
from src.brreg import BrregClient  # noqa: E402
from src.session_token import session_settings  # noqa: E402

PDF = b"%PDF-1.4 fake"
# What Brreg's /regnskap answers for a bank, every time (seen for DNB BANK ASA, 2026-10-26)
BANK_500 = {
    "status": "500",
    "error": "Internal Server Error",
    "message": "Regnskapet inneholder en oppstillingsplan som ikke er stottet (BANK)",
}
TEST_EMAIL = "journalist@example.com"


def session_cookie(email: str = TEST_EMAIL) -> str:
    """A v2 session token for `email`, signed with the test key."""
    claims = {"user_id": 1, "email": email}
    return sign_v2(
        claims, parse_signing_key(TEST_SIGNING_KEY), issuer=TEST_ISSUER, now=int(time.time())
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def signed_claims(claims: dict) -> str:
    """A v2 token over any claims, signed with the test key past sign_v2's own claim checks."""
    now = int(time.time())
    body = {"iss": TEST_ISSUER, "kid": "ukd-test", **claims, "iat": now, "exp": now + 3600}
    payload = _b64(json.dumps(body).encode())
    sig = parse_signing_key(TEST_SIGNING_KEY).key.sign(f"v2.{payload}".encode("ascii"))
    return f"v2.{payload}.{_b64(sig)}"


@pytest.fixture(autouse=True)
def fresh_session_settings():
    """Each test reads SESSION_* from its own environment (tests may monkeypatch it)."""
    session_settings.cache_clear()
    yield
    session_settings.cache_clear()


class FakeBrreg:
    """In-memory stand-in for Brreg, served through one respx route. Mimics observed responses:
    unknown orgnr → 404 on /enheter and PDFs, `[]` on the years list."""

    def __init__(self) -> None:
        self.enheter: dict[str, dict] = {}
        self.underenheter: dict[str, dict] = {}
        self.years: dict[str, list[int]] = {}
        self.pdf_status: dict[tuple[str, int], int] = {}
        self.figures: dict[str, list[dict]] = {}
        self.search_rows: list[dict] = []
        self.roles: dict[str, dict] = {}
        self.queued: list[httpx.Response] = []  # served first, e.g. to simulate 429s
        self.pdf_requests: list[tuple[str, int]] = []
        self.pdf_bytes: dict[tuple[str, int], bytes] = {}  # real PDFs for some years; PDF otherwise
        self.banks: set[str] = set()  # /regnskap answers these with Brreg's 500 for bank layouts
        self.paths: list[str] = []  # every request, in order

    def company(
        self, orgnr: str, name: str, years: list[int], missing_pdfs: tuple[int, ...] = (), **extra
    ) -> None:
        self.enheter[orgnr] = {
            "organisasjonsnummer": orgnr,
            "navn": name,
            "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
            **extra,
        }
        self.years[orgnr] = years
        for y in years:
            self.pdf_status[(orgnr, y)] = 404 if y in missing_pdfs else 200

    def underenhet(self, orgnr: str, name: str, parent: str) -> None:
        self.underenheter[orgnr] = {
            "organisasjonsnummer": orgnr,
            "navn": name,
            "overordnetEnhet": parent,
            "organisasjonsform": {"kode": "BEDR"},
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if self.queued:
            return self.queued.pop(0)
        if m := re.fullmatch(r"/enhetsregisteret/api/enheter/(\d+)/roller", path):
            return (
                httpx.Response(200, json=self.roles[m[1]])
                if m[1] in self.roles
                else httpx.Response(404)
            )
        if m := re.fullmatch(r"/enhetsregisteret/api/enheter", path):
            name = request.url.params.get("navn", "")
            rows = [r for r in self.search_rows if name.lower() in r["navn"].lower()]
            return httpx.Response(
                200, json={"_embedded": {"enheter": rows}} if rows else {"page": {}}
            )
        if m := re.fullmatch(r"/enhetsregisteret/api/(enheter|underenheter)/(\d+)", path):
            table = self.enheter if m[1] == "enheter" else self.underenheter
            return httpx.Response(200, json=table[m[2]]) if m[2] in table else httpx.Response(404)
        if m := re.fullmatch(r"/regnskapsregisteret/regnskap/aarsregnskap/kopi/(\d+)/aar", path):
            return httpx.Response(200, json=[str(y) for y in self.years.get(m[1], [])])
        if m := re.fullmatch(r"/regnskapsregisteret/regnskap/aarsregnskap/kopi/(\d+)/(\d+)", path):
            key = (m[1], int(m[2]))
            self.pdf_requests.append(key)
            status = self.pdf_status.get(key, 404)
            return httpx.Response(
                status, content=self.pdf_bytes.get(key, PDF) if status == 200 else b""
            )
        if m := re.fullmatch(r"/regnskapsregisteret/regnskap/(\d+)", path):
            if m[1] in self.banks:
                return httpx.Response(500, json=BANK_500)
            return (
                httpx.Response(200, json=self.figures[m[1]])
                if m[1] in self.figures
                else httpx.Response(404)
            )
        return httpx.Response(418)


@pytest.fixture
def brreg():
    fake = FakeBrreg()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="data.brreg.no").mock(side_effect=fake.handle)
        yield fake


@pytest.fixture
async def client_brreg():
    async with httpx.AsyncClient() as http:
        yield BrregClient(http, backoff=0)


@pytest.fixture(autouse=True)
def _job_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("REGNSKAP_DATA_DIR", str(tmp_path / "data"))
    jobs.JOBS.clear()
    subsidiaries._running.clear()
    limits.reset()
    db.init_db()


class FakeAI:
    """Stands in for Gemini: canned extraction result and chat answer, records calls."""

    def __init__(self) -> None:
        self.extraction = subsidiaries.Extraction(note_found=False)
        self.answer = "Svar fra test."
        self.pdfs: list[bytes] = []
        self.systems: list[str] = []

    async def extract(self, pdf: bytes) -> subsidiaries.Extraction:
        self.pdfs.append(pdf)
        return self.extraction

    async def chat(self, system: str, messages: list[dict]) -> str:
        self.systems.append(system)
        return self.answer


@pytest.fixture
def ai():
    return FakeAI()


class FakeReader:
    """Stands in for the page-read models: an empty but valid answer per schema; records each call's schema."""

    def __init__(self, key: bool = True) -> None:
        self.calls: list[str] = []
        self.key = key

    def chain(self) -> list[str]:
        if not self.key:
            raise MissingKey(NO_KEY)
        return ["fake/model"]

    async def read(self, img: bytes, prompt: str, schema, reasoning: str = "none"):
        self.calls.append(schema.__name__)
        empty = {
            "Page": {
                "page_kind": "annet",
                "is_brreg_printout": False,
                "figures_for": "ingen",
                "unit_scale": 1,
                "current_year": None,
                "prior_year": None,
                "statement_lines": [],
                "notes": [],
                "inline_amounts": [],
            },
            "NumberList": {"numbers": []},
            "PageText": {"blocks": []},
        }[schema.__name__]
        return schema.model_validate(empty), Usage(model="fake/model", cost=0.001)

    async def aclose(self) -> None:
        pass


@pytest.fixture
def reader():
    return FakeReader()


@pytest.fixture
def app_client(brreg, ai, reader):
    """TestClient with a valid session cookie and a no-backoff Brreg client."""
    from fastapi.testclient import TestClient

    from src.main import app

    with TestClient(
        app, headers={"Cookie": f"session={session_cookie()}"}, follow_redirects=False
    ) as c:
        app.state.brreg.backoff = 0
        app.state.extractor = ai.extract
        app.state.chat_model = ai.chat
        app.state.reader = reader
        yield c

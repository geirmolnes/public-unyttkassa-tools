"""Read the subsidiaries note out of an annual-report PDF with Gemini and store it as ownership."""

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from src import db, usage
from src.brreg import BrregClient
from src.parsing import orgnr_error

log = logging.getLogger(__name__)

MODEL = "gemini-3.5-flash-lite"
MAX_OUTPUT_TOKENS = 65536
RESOLVE_CONCURRENCY = 4
NORWAY = {"norge", "norway", "no"}
NO_KEY = "AI er ikke konfigurert (mangler GEMINI_API_KEY)."
READ_FAILED = "Kunne ikke lese årsrapporten. Prøv igjen senere."

PROMPT = """Dette er et norsk årsregnskap/årsrapport som PDF.
Finn noten som lister selskapets datterselskaper, for eksempel «Selskaper inkludert i
konsolideringen», «Datterselskaper», «Investering i datterselskap» eller «Konsernselskaper».
Ta med HVER rad i listen, også når den går over mange sider. Ikke slå sammen, ikke forkort,
ikke hopp over rader. Gjengi navnet nøyaktig slik det står.

Ta bare med selskaper som er datterselskaper ved utgangen av regnskapsåret. Ikke ta med lister over
selskaper som er fusjonert inn i selskapet («innfusjonerte selskaper», «fusjon»), solgt, avviklet
eller bare tilknyttede selskaper, selv om de står med eierandel.

For hver rad: name (juridisk navn), orgnr (organisasjonsnummer, 9 siffer, bare hvis det står i
raden), country (land, f.eks. «Norge»), place (kontorsted/forretningskontor), share_pct (eierandel i
prosent som tall, f.eks. 100), page (sidetall i PDF-filen der raden står, 1 = første side i filen).
Felter som ikke står i listen settes til null.
Hvis dokumentet ikke har en slik liste: note_found = false og rows = []."""


class SubsidiaryRow(BaseModel):
    name: str
    orgnr: str | None = None
    country: str | None = None
    place: str | None = None
    share_pct: float | None = None
    page: int | None = None


class Extraction(BaseModel):
    note_found: bool
    rows: list[SubsidiaryRow] = Field(default_factory=list)


Extractor = Callable[[bytes], Awaitable[Extraction]]


class MissingKey(Exception):
    """GEMINI_API_KEY is not set."""


def _client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise MissingKey(NO_KEY)
    return genai.Client(api_key=key)


async def gemini_extract(pdf: bytes) -> Extraction:
    """Default extractor: one structured-output call on the inline PDF."""
    client = _client()
    try:
        resp = await client.aio.models.generate_content(
            model=MODEL,
            contents=[types.Part.from_bytes(data=pdf, mime_type="application/pdf"), PROMPT],
            config=types.GenerateContentConfig(
                temperature=0,
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                response_mime_type="application/json",
                response_schema=Extraction,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )
    except Exception:
        await usage.gemini(None, MODEL, "subsidiaries", "worker", "multimodal", status="error")
        raise
    await usage.gemini(resp, MODEL, "subsidiaries", "worker", "multimodal")
    if isinstance(resp.parsed, Extraction):
        return resp.parsed
    return Extraction.model_validate_json(resp.text or "")


def _is_norwegian(country: str | None) -> bool:
    return country is None or country.strip().lower() in NORWAY


def _printed_orgnr(row: SubsidiaryRow) -> str | None:
    """The orgnr the report prints for this row, if it reads as a valid one."""
    digits = re.sub(r"\s+", "", row.orgnr or "")
    return digits if digits and orgnr_error(digits) is None else None


async def _official_name(client: BrregClient, orgnr: str) -> str | None:
    """Brreg's registered name for `orgnr`; None if Brreg doesn't know it. Raises on Brreg errors."""
    info = await client.get_company(orgnr)
    return info.name if info else None


async def _resolve(client: BrregClient, rows: list[SubsidiaryRow]) -> list[dict[str, Any]]:
    """Attach orgnr and show Brreg's registered name for linked rows (scanned reports garble names).

    The orgnr printed in the report wins when Brreg knows it; an unknown one is a misread that
    passed the checksum, so Norwegian rows then fall back to an exact name match."""
    slots = asyncio.Semaphore(RESOLVE_CONCURRENCY)

    async def one(row: SubsidiaryRow) -> dict[str, Any]:
        out = row.model_dump()
        out["orgnr"] = None
        async with slots:
            try:
                if printed := _printed_orgnr(row):
                    if name := await _official_name(client, printed):
                        return {**out, "orgnr": printed, "name": name}
                if not _is_norwegian(row.country):
                    return out
                if found := await client.find_exact(row.name):
                    out["orgnr"] = found
                    out["name"] = await _official_name(client, found) or row.name
            except Exception:  # best effort: a failed lookup keeps the report's own row
                log.warning("orgnr lookup failed for %r", row.name, exc_info=True)
                out["orgnr"] = out["orgnr"] or _printed_orgnr(row)
        return out

    return await asyncio.gather(*(one(r) for r in rows))


_running: set[str] = set()


async def run_extraction(client: BrregClient, orgnr: str, year: int, extractor: Extractor) -> None:
    """Fetch the PDF, extract, resolve orgnrs and persist. Status lands in `extractions`."""
    try:
        pdf = await client.fetch_pdf(orgnr, year)
        result = await extractor(pdf)
    except MissingKey as e:
        db.finish_extraction(orgnr, year, "failed", error=str(e))
        return
    except Exception:
        log.exception("subsidiary extraction failed for %s/%s", orgnr, year)
        db.finish_extraction(orgnr, year, "failed", error=READ_FAILED)
        return

    rows = [r for r in result.rows if r.name.strip()]
    if not result.note_found or not rows:
        db.replace_ownership(orgnr, year, [], MODEL)
        db.finish_extraction(orgnr, year, "none", n_rows=0)
        return
    resolved = await _resolve(client, rows)
    db.replace_ownership(orgnr, year, resolved, MODEL)
    db.finish_extraction(orgnr, year, "ok", n_rows=len(resolved))


def start(
    client: BrregClient, orgnr: str, year: int, extractor: Extractor, tasks: set[asyncio.Task]
) -> bool:
    """Schedule an extraction unless one is already running for `orgnr`. Returns True if started."""
    if orgnr in _running:
        return False
    _running.add(orgnr)
    db.start_extraction(orgnr, year, MODEL)

    async def go() -> None:
        try:
            await run_extraction(client, orgnr, year, extractor)
        finally:
            _running.discard(orgnr)

    task = asyncio.create_task(go())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return True


def status(orgnr: str) -> dict[str, Any]:
    """`Subsidiaries` shape from the contract, for the newest extraction of `orgnr`."""
    ex = db.latest_extraction(orgnr)
    if not ex:
        return {"status": "none_yet", "year": None, "rows": [], "error": None, "model": None}
    rows = db.subsidiaries_of(orgnr, ex["year"]) if ex["status"] == "ok" else []
    return {
        "status": ex["status"],
        "year": ex["year"],
        "rows": rows,
        "error": ex["error"],
        "model": ex["model"],
    }

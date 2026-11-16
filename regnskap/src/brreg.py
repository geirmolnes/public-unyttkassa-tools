"""Thin async client for Brønnøysundregistrene's open APIs (no auth needed)."""

import asyncio
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from src.parsing import normalize_orgnr

ENHET_BASE = "https://data.brreg.no/enhetsregisteret/api"
REGNSKAP_BASE = "https://data.brreg.no/regnskapsregisteret/regnskap"

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
# JSON lookups answer in ~40 ms (p90 60 ms, max 0.5 s measured): a hung one is retried long before
# the 60 s that PDFs and the kunngjøring pages get.
LOOKUP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# /regnskap answers 500 for accounts in a layout it can't serve (banks), every time
UNSUPPORTED = "oppstillingsplan som ikke er stottet"
PDF_CONCURRENCY = 3
CACHE_TTL = 600.0


class BrregUnavailable(Exception):
    """Brreg did not answer usefully after retries."""


class NotAvailable(Exception):
    """The requested document does not exist (404)."""


@dataclass
class Company:
    orgnr: str
    name: str
    form_code: str
    form_desc: str
    place: str
    kind: str  # "enhet" | "slettet" | "underenhet"
    konkurs: bool = False
    under_avvikling: bool = False
    deleted_date: str | None = None
    parent_orgnr: str | None = None
    last_report_year: str | None = None
    in_group: bool = False
    registered: str | None = None
    industry: str | None = None
    capital: dict[str, Any] | None = None
    employees: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_company(data: dict[str, Any], kind: str = "enhet") -> Company:
    """Map an Enhetsregisteret JSON object to Company."""
    if data.get("respons_klasse") == "SlettetEnhet" or data.get("slettedato"):
        kind = "slettet"
    form = data.get("organisasjonsform") or {}
    addr = (
        data.get("forretningsadresse")
        or data.get("beliggenhetsadresse")
        or data.get("postadresse")
        or {}
    )
    industry = (data.get("naeringskode1") or {}).get("beskrivelse")
    kapital = data.get("kapital") or {}
    capital = (
        {
            "amount": kapital.get("belop"),
            "shares": kapital.get("antallAksjer"),
            "currency": kapital.get("valuta") or "NOK",
        }
        if kapital.get("belop") is not None
        else None
    )
    return Company(
        orgnr=data["organisasjonsnummer"],
        name=data.get("navn") or data["organisasjonsnummer"],
        form_code=form.get("kode", ""),
        form_desc=form.get("beskrivelse", ""),
        place=addr.get("poststed") or "",
        kind=kind,
        konkurs=bool(data.get("konkurs")),
        under_avvikling=bool(
            data.get("underAvvikling") or data.get("underTvangsavviklingEllerTvangsopplosning")
        ),
        deleted_date=data.get("slettedato"),
        parent_orgnr=data.get("overordnetEnhet"),
        last_report_year=data.get("sisteInnsendteAarsregnskap"),
        in_group=bool(data.get("erIKonsern")),
        registered=data.get("registreringsdatoEnhetsregisteret"),
        industry=industry,
        capital=capital,
        employees=data.get("antallAnsatte"),
    )


def _num(data: dict[str, Any], *path: str) -> float | None:
    """Nested lookup; None if any step is missing."""
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data if isinstance(data, int | float) else None


def parse_key_figures_history(
    rows: list[dict[str, Any]], kind: str = "SELSKAP"
) -> list[dict[str, Any]]:
    """All years of one accounts type (SELSKAP or KONSERN) from /regnskap/<orgnr>, flattened, newest first."""
    years = [_flatten(r) for r in rows if r.get("regnskapstype") == kind]
    # Brreg sends oldest first, undocumented; sort so the order never decides "latest".
    return sorted(years, key=lambda kf: kf["to_date"] or "", reverse=True)


def parse_key_figures(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Newest company-level year, or None."""
    history = parse_key_figures_history(rows)
    return history[0] if history else None


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    """One /regnskap row as a flat dict of the figures the UI shows."""
    res = row.get("resultatregnskapResultat") or {}
    ekg = row.get("egenkapitalGjeld") or {}
    eiendeler = row.get("eiendeler") or {}
    period = row.get("regnskapsperiode") or {}
    revisjon = row.get("revisjon") or {}
    prinsipper = row.get("regnkapsprinsipper") or row.get("regnskapsprinsipper") or {}
    to_date = period.get("tilDato") or ""

    resultat_for_skatt = _num(res, "ordinaertResultatFoerSkattekostnad")
    aarsresultat = _num(res, "aarsresultat")
    egenkapital = _num(ekg, "egenkapital", "sumEgenkapital")
    sum_eiendeler = _num(eiendeler, "sumEiendeler")
    revidert = None
    if "ikkeRevidertAarsregnskap" in revisjon or "fravalgRevisjon" in revisjon:
        revidert = not (revisjon.get("ikkeRevidertAarsregnskap") or revisjon.get("fravalgRevisjon"))
    return {
        "year": int(to_date[:4]) if to_date[:4].isdigit() else None,
        "from_date": period.get("fraDato"),
        "to_date": to_date or None,
        "currency": row.get("valuta") or "NOK",
        "driftsinntekter": _num(res, "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
        "salgsinntekter": _num(res, "driftsresultat", "driftsinntekter", "salgsinntekter"),
        "driftskostnader": _num(res, "driftsresultat", "driftskostnad", "sumDriftskostnad"),
        "loennskostnad": _num(res, "driftsresultat", "driftskostnad", "loennskostnad"),
        "driftsresultat": _num(res, "driftsresultat", "driftsresultat"),
        "finansinntekter": _num(res, "finansresultat", "finansinntekt", "sumFinansinntekter"),
        "finanskostnader": _num(res, "finansresultat", "finanskostnad", "sumFinanskostnad"),
        "rentekostnad_konsern": _num(
            res, "finansresultat", "finanskostnad", "rentekostnadSammeKonsern"
        ),
        "rentekostnader": _num(res, "finansresultat", "finanskostnad", "annenRentekostnad"),
        "netto_finans": _num(res, "finansresultat", "nettoFinans"),
        "resultat_for_skatt": resultat_for_skatt,
        "skattekostnad": _num(res, "ordinaertResultatSkattekostnad"),
        "ekstraordinaere_poster": _num(res, "ekstraordinaerePoster"),
        "skatt_ekstraordinaert": _num(res, "skattekostnadEkstraordinaertResultat"),
        "skattekostnad_avledet": (
            resultat_for_skatt - aarsresultat
            if resultat_for_skatt is not None and aarsresultat is not None
            else None
        ),
        "aarsresultat": aarsresultat,
        "totalresultat": _num(res, "totalresultat"),
        "anleggsmidler": _num(eiendeler, "anleggsmidler", "sumAnleggsmidler"),
        "goodwill": _num(eiendeler, "goodwill"),
        "omlopsmidler": _num(eiendeler, "omloepsmidler", "sumOmloepsmidler"),
        "varer": _num(eiendeler, "sumVarer"),
        "fordringer": _num(eiendeler, "sumFordringer"),
        "investeringer": _num(eiendeler, "sumInvesteringer"),
        "bankinnskudd": _num(eiendeler, "sumBankinnskuddOgKontanter"),
        "sum_eiendeler": sum_eiendeler,
        # Brreg spells this key "Egenkaptial"; accept both in case they fix it.
        "innskutt_egenkapital": _num(
            ekg, "egenkapital", "innskuttEgenkapital", "sumInnskuttEgenkaptial"
        )
        or _num(ekg, "egenkapital", "innskuttEgenkapital", "sumInnskuttEgenkapital"),
        "opptjent_egenkapital": _num(
            ekg, "egenkapital", "opptjentEgenkapital", "sumOpptjentEgenkapital"
        ),
        "egenkapital": egenkapital,
        "langsiktig_gjeld": _num(ekg, "gjeldOversikt", "langsiktigGjeld", "sumLangsiktigGjeld"),
        "kortsiktig_gjeld": _num(ekg, "gjeldOversikt", "kortsiktigGjeld", "sumKortsiktigGjeld"),
        "sum_gjeld": _num(ekg, "gjeldOversikt", "sumGjeld"),
        "ek_andel": (
            egenkapital / sum_eiendeler if egenkapital is not None and sum_eiendeler else None
        ),
        "revidert": revidert,
        "smaa_foretak": prinsipper.get("smaaForetak"),
        "regnskapsregler": prinsipper.get("regnskapsregler"),
        "morselskap": (row.get("virksomhet") or {}).get("morselskap"),
        "avviklingsregnskap": row.get("avviklingsregnskap") is True,
        # Kept from an earlier visit; Brreg no longer serves this year (src/archive.py).
        "archived": row.get("_archived") is True,
    }


def parse_roles(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten /roller into [{role, name, orgnr}], skipping resigned (fratraadt) roles."""
    out = []
    for group in data.get("rollegrupper") or []:
        for r in group.get("roller") or []:
            if r.get("fratraadt"):
                continue
            person = r.get("person") or {}
            enhet = r.get("enhet") or {}
            navn = person.get("navn") or {}
            name = " ".join(
                p for p in (navn.get("fornavn"), navn.get("mellomnavn"), navn.get("etternavn")) if p
            ) or " ".join(enhet.get("navn") or [])
            if not name:
                continue
            out.append(
                {
                    "role": (r.get("type") or {}).get("beskrivelse") or "",
                    "name": name,
                    "orgnr": enhet.get("organisasjonsnummer"),
                }
            )
    return out


def normalize_name(name: str) -> str:
    """Comparable company name: NFC, upper case, punctuation dropped, single spaces."""
    s = unicodedata.normalize("NFC", name or "").upper()
    s = re.sub(r"[^\w\s&-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class BrregClient:
    """Brreg API wrapper with retries, a small TTL cache and a global PDF concurrency cap."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        backoff: float = 1.0,
        on_pdf: Callable[[str, int, bytes], Awaitable[None]] | None = None,
    ) -> None:
        self.http = http
        self.on_pdf = on_pdf  # called with every PDF fetched (src/archive.py keeps them)
        self.backoff = backoff
        self._pdf_slots = asyncio.Semaphore(PDF_CONCURRENCY)
        self._cache: dict[str, tuple[float, Any]] = {}
        # Brreg's JSON as received, per "source:orgnr", for src/archive.py to keep (see raw()).
        self._raw: dict[str, Any] = {}

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        timeout: httpx.Timeout = LOOKUP_TIMEOUT,
    ) -> httpx.Response:
        """GET with retry on 429/5xx/network errors. Returns 2xx/4xx responses, and a 5xx that
        retrying can't fix, as-is."""
        for attempt in range(MAX_ATTEMPTS):
            last = attempt == MAX_ATTEMPTS - 1
            try:
                resp = await self.http.get(url, params=params, timeout=timeout)
            except httpx.TransportError as e:
                if last:
                    raise BrregUnavailable(f"Nettverksfeil mot Brreg: {type(e).__name__}") from e
                await asyncio.sleep(self.backoff * 2**attempt)
                continue
            if resp.status_code not in RETRY_STATUSES or UNSUPPORTED in resp.text:
                return resp
            if last:
                raise BrregUnavailable(f"Brreg svarte {resp.status_code}")
            await asyncio.sleep(_retry_after(resp) or self.backoff * 2**attempt)
        raise AssertionError("unreachable")

    async def get_html(self, url: str) -> str:
        """A public HTML page (the kunngjøring search), decoded as the server says (ISO-8859-1)."""
        resp = await self._get(url, timeout=DOWNLOAD_TIMEOUT)
        if resp.status_code != 200:
            raise BrregUnavailable(f"Brreg svarte {resp.status_code}")
        return resp.text

    async def _cached(self, key: str, loader):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        value = await loader()
        if len(self._cache) > 2000:
            self._cache.clear()
            self._raw.clear()
        self._cache[key] = (time.monotonic(), value)
        return value

    async def search(self, query: str, size: int = 10) -> list[Company]:
        """Org number → direct lookup (also finds deleted units); otherwise name search."""
        if orgnr := normalize_orgnr(query):
            company = await self.get_company(orgnr)
            return [company] if company else []
        resp = await self._get(f"{ENHET_BASE}/enheter", {"navn": query, "size": size})
        if resp.status_code == 400:
            return []
        resp.raise_for_status()
        rows = resp.json().get("_embedded", {}).get("enheter", [])
        return [parse_company(r) for r in rows]

    async def get_company(self, orgnr: str) -> Company | None:
        """Main unit (incl. deleted) or, failing that, a sub-unit (underenhet)."""

        async def load() -> Company | None:
            resp = await self._get(f"{ENHET_BASE}/enheter/{orgnr}")
            if resp.status_code == 200:
                self._raw[f"enhet:{orgnr}"] = resp.json()
                return parse_company(resp.json())
            if resp.status_code not in (404, 410):
                resp.raise_for_status()
            sub = await self._get(f"{ENHET_BASE}/underenheter/{orgnr}")
            if sub.status_code == 200:
                self._raw[f"underenhet:{orgnr}"] = sub.json()
                return parse_company(sub.json(), kind="underenhet")
            return None

        return await self._cached(f"company:{orgnr}", load)

    async def list_years(self, orgnr: str) -> list[int]:
        """Years with a PDF copy of the annual accounts. Unknown orgnr gives []."""

        async def load() -> list[int]:
            resp = await self._get(f"{REGNSKAP_BASE}/aarsregnskap/kopi/{orgnr}/aar")
            if resp.status_code in (400, 404):
                return []
            resp.raise_for_status()
            return sorted(int(y) for y in resp.json() if str(y).isdigit())

        return await self._cached(f"years:{orgnr}", load)

    async def figure_rows(self, orgnr: str) -> list[dict[str, Any]]:
        """Raw /regnskap rows (every year, SELSKAP and KONSERN). [] if Brreg has none or can't serve them."""

        async def load() -> list[dict[str, Any]]:
            resp = await self._get(f"{REGNSKAP_BASE}/{orgnr}")
            return resp.json() if resp.status_code == 200 else []

        return await self._cached(f"figures:{orgnr}", load)

    async def key_figures_history(self, orgnr: str, kind: str = "SELSKAP") -> list[dict[str, Any]]:
        """Every year Brreg has as data for one accounts type, newest first."""
        return parse_key_figures_history(await self.figure_rows(orgnr), kind)

    def raw(self, orgnr: str) -> dict[str, Any]:
        """Brreg's JSON for `orgnr` as last received (unit or sub-unit, roles), by source."""
        return {
            src: self._raw[f"{src}:{orgnr}"]
            for src in ("enhet", "underenhet", "roller")
            if f"{src}:{orgnr}" in self._raw
        }

    async def key_figures(self, orgnr: str) -> dict[str, Any] | None:
        """Newest year's key figures (company accounts only). None if not available."""
        history = await self.key_figures_history(orgnr)
        return history[0] if history else None

    async def roles(self, orgnr: str) -> list[dict[str, Any]]:
        """Current roles (styreleder, daglig leder, revisor …). Empty if Brreg has none."""

        async def load() -> list[dict[str, Any]]:
            resp = await self._get(f"{ENHET_BASE}/enheter/{orgnr}/roller")
            if resp.status_code != 200:
                return []
            self._raw[f"roller:{orgnr}"] = resp.json()
            return parse_roles(resp.json())

        return await self._cached(f"roles:{orgnr}", load)

    async def find_exact(self, name: str) -> str | None:
        """Orgnr of the single active unit whose name matches `name` exactly (normalized)."""
        target = normalize_name(name)
        if not target:
            return None

        async def load() -> str | None:
            resp = await self._get(f"{ENHET_BASE}/enheter", {"navn": name, "size": 20})
            if resp.status_code != 200:
                return None
            rows = resp.json().get("_embedded", {}).get("enheter", [])
            hits = {
                r["organisasjonsnummer"]
                for r in rows
                if normalize_name(r.get("navn", "")) == target
            }
            return hits.pop() if len(hits) == 1 else None

        return await self._cached(f"exact:{target}", load)

    async def fetch_pdf(self, orgnr: str, year: int) -> bytes:
        """Download one annual-accounts PDF. Raises NotAvailable / BrregUnavailable."""
        async with self._pdf_slots:
            resp = await self._get(
                f"{REGNSKAP_BASE}/aarsregnskap/kopi/{orgnr}/{year}", timeout=DOWNLOAD_TIMEOUT
            )
        if resp.status_code == 404:
            raise NotAvailable(f"Brreg har ingen kopi av årsregnskapet for {year}")
        if resp.status_code != 200:
            raise BrregUnavailable(f"Brreg svarte {resp.status_code}")
        if not resp.content.startswith(b"%PDF"):
            raise BrregUnavailable("Brreg sendte noe som ikke er en PDF")
        if self.on_pdf:
            await self.on_pdf(orgnr, year, resp.content)
        return resp.content


def _retry_after(resp: httpx.Response) -> float | None:
    """Seconds Brreg asks us to wait. The accounts register's 429 (30 requests per window to
    aarsregnskap/kopi, years list and PDFs alike) says so in its own header, not Retry-After."""
    value = resp.headers.get("retry-after") or resp.headers.get(
        "x-rate-limit-retry-after-seconds", ""
    )
    return min(float(value), 30.0) if value.isdigit() else None

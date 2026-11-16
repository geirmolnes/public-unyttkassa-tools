"""Multi-file download jobs: resolve companies, fetch PDFs, write a ZIP to disk."""

import asyncio
import os
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from src.brreg import BrregClient, BrregUnavailable, Company, NotAvailable
from src.naming import batch_zip_filename, company_folder, pdf_filename, zip_filename
from src.parsing import SelectionMode, select_years

MAX_ORGNRS = 50
MAX_ORGNR_TEXT = 50_000  # chars of pasted input; room for 50 orgnrs with names and noise
MAX_FILES = 300
MAX_ACTIVE_PER_USER = 3
MAX_ACTIVE = 10
MAX_REPORTED_INVALID = 200  # invalid input lines kept as failures; the rest become one summary line
LOOKUP_CONCURRENCY = 4
JOB_TTL = 3600.0
REPORT_NAME = "MANGLER.txt"


@dataclass
class Selection:
    mode: SelectionMode
    years: list[int] | None = None
    last_n: int | None = None
    from_year: int | None = None
    to_year: int | None = None

    def apply(self, available: list[int]) -> list[int]:
        return select_years(
            available,
            self.mode,
            years=self.years,
            last_n=self.last_n,
            from_year=self.from_year,
            to_year=self.to_year,
        )


@dataclass
class Failure:
    orgnr: str
    reason: str
    name: str | None = None
    year: int | None = None


@dataclass
class CompanyResult:
    orgnr: str
    name: str
    years: list[int]
    ok_years: list[int] = field(default_factory=list)


@dataclass
class Job:
    id: str
    owner: str
    orgnrs: list[str]
    selection: Selection
    batch: bool
    phase: str = "resolving"  # resolving | downloading | done | failed
    error: str | None = None
    companies_done: int = 0
    total: int = 0
    done: int = 0
    failures: list[Failure] = field(default_factory=list)
    companies: list[CompanyResult] = field(default_factory=list)
    filename: str | None = None
    path: Path | None = None
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase": self.phase,
            "error": self.error,
            "companies_total": len(self.orgnrs),
            "companies_done": self.companies_done,
            "total": self.total,
            "done": self.done,
            "ok_files": sum(len(c.ok_years) for c in self.companies),
            "filename": self.filename,
            "failures": [f.__dict__ for f in self.failures],
            "companies": [c.__dict__ for c in self.companies],
        }


JOBS: dict[str, Job] = {}


def active(owner: str | None = None) -> int:
    """Jobs still resolving or downloading, for one owner or for everyone."""
    return sum(
        j.phase in ("resolving", "downloading") and (owner is None or j.owner == owner)
        for j in JOBS.values()
    )


def invalid_failures(invalid: list[tuple[str, str]]) -> list[Failure]:
    """Failures for invalid input lines, at most MAX_REPORTED_INVALID plus a count of the rest."""
    failures = [Failure(raw, reason) for raw, reason in invalid[:MAX_REPORTED_INVALID]]
    if (rest := len(invalid) - MAX_REPORTED_INVALID) > 0:
        failures.append(Failure("…", f"og {rest} linjer til som ikke er organisasjonsnummer"))
    return failures


def job_dir() -> Path:
    path = Path(os.environ.get("JOB_DIR") or Path(tempfile.gettempdir()) / "unytt_regnskap")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_jobs(now: float | None = None) -> None:
    """Drop jobs (and their ZIPs) older than JOB_TTL."""
    now = now or time.time()
    for job_id, job in list(JOBS.items()):
        if now - job.created < JOB_TTL:
            continue
        if job.path:
            job.path.unlink(missing_ok=True)
        del JOBS[job_id]


def create_job(owner: str, orgnrs: list[str], selection: Selection, batch: bool) -> Job:
    """Register a job; run it with `run_job`."""
    cleanup_jobs()
    job = Job(id=uuid.uuid4().hex, owner=owner, orgnrs=orgnrs, selection=selection, batch=batch)
    JOBS[job.id] = job
    return job


async def _resolve(
    client: BrregClient, job: Job, orgnr: str, slots: asyncio.Semaphore
) -> tuple[Company, list[int]] | None:
    """Look up one company + its selected years; records a Failure and returns None if unusable."""
    async with slots:
        try:
            company = await client.get_company(orgnr)
            available = await client.list_years(orgnr) if company else []
        except (BrregUnavailable, httpx.HTTPError) as e:
            job.failures.append(Failure(orgnr, f"Kunne ikke slå opp selskapet ({e})"))
            return None
        finally:
            job.companies_done += 1
    if company is None:
        job.failures.append(Failure(orgnr, "Finnes ikke i Enhetsregisteret"))
        return None
    if company.kind == "underenhet":
        reason = "Er en underenhet – årsregnskap ligger på hovedenheten"
        if company.parent_orgnr:
            reason += f" ({company.parent_orgnr})"
        job.failures.append(Failure(orgnr, reason, company.name))
        return None
    if not available:
        job.failures.append(Failure(orgnr, "Brreg har ingen kopier av årsregnskap", company.name))
        return None
    chosen = job.selection.apply(available)
    if job.selection.mode == "years":
        for year in sorted(set(job.selection.years or []) - set(available)):
            job.failures.append(Failure(orgnr, "Ikke tilgjengelig hos Brreg", company.name, year))
    if not chosen:
        job.failures.append(Failure(orgnr, "Ingen tilgjengelige år i valgt utvalg", company.name))
        return None
    return company, chosen


def _arcname(job: Job, company: Company, year: int) -> str:
    name = pdf_filename(company.name, year)
    return f"{company_folder(company.name, company.orgnr)}/{name}" if job.batch else name


def _report(job: Job) -> str:
    lines = ["Disse årsregnskapene kom ikke med:", ""]
    for f in job.failures:
        who = f"{f.name} ({f.orgnr})" if f.name else f.orgnr
        year = f" {f.year}" if f.year else ""
        lines.append(f"- {who}{year}: {f.reason}")
    return "\n".join(lines) + "\n"


async def run_job(client: BrregClient, job: Job, today: date | None = None) -> None:
    """Resolve all companies, download selected PDFs (bounded concurrency) into a ZIP."""
    slots = asyncio.Semaphore(LOOKUP_CONCURRENCY)
    resolved = await asyncio.gather(*(_resolve(client, job, o, slots) for o in job.orgnrs))
    plan = [r for r in resolved if r]
    job.companies = [CompanyResult(c.orgnr, c.name, years) for c, years in plan]
    job.total = sum(len(years) for _, years in plan)
    if not plan:
        job.phase, job.error = "failed", "Fant ingen årsregnskap å laste ned"
        return
    if job.total > MAX_FILES:
        job.phase = "failed"
        job.error = f"For mange filer ({job.total}). Maks {MAX_FILES} per nedlasting – velg færre år eller selskaper."
        return

    if job.batch:
        job.filename = batch_zip_filename(len(plan), today or date.today())
    else:
        job.filename = zip_filename(plan[0][0].name, plan[0][1])
    job.path = job_dir() / f"{job.id}.zip"
    job.phase = "downloading"
    results = {c.orgnr: c for c in job.companies}

    async def fetch(company: Company, year: int) -> tuple[Company, int, bytes | None]:
        try:
            return company, year, await client.fetch_pdf(company.orgnr, year)
        except (NotAvailable, BrregUnavailable, httpx.HTTPError) as e:
            job.failures.append(Failure(company.orgnr, str(e), company.name, year))
            return company, year, None
        finally:
            job.done += 1

    tasks = [fetch(c, y) for c, years in plan for y in years]
    with zipfile.ZipFile(job.path, "w", zipfile.ZIP_STORED) as zf:
        for next_done in asyncio.as_completed(tasks):
            company, year, data = await next_done
            if data is None:
                continue
            zf.writestr(_arcname(job, company, year), data)
            results[company.orgnr].ok_years.append(year)
        for c in job.companies:
            c.ok_years.sort()
        if job.failures:
            zf.writestr(REPORT_NAME, _report(job))

    if not any(c.ok_years for c in job.companies):
        job.path.unlink(missing_ok=True)
        job.path = None
        job.phase, job.error = "failed", "Ingen av årsregnskapene kunne lastes ned"
        return
    job.phase = "done"

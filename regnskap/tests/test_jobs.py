"""ZIP assembly: run_job against a mocked Brreg, then inspect the archive."""

import zipfile
from datetime import date

import pytest

from src import jobs
from src.jobs import Selection

TODAY = date(2026, 9, 30)


async def _run(client_brreg, orgnrs, selection, batch=False):
    job = jobs.create_job("a@b.no", orgnrs, selection, batch=batch)
    await jobs.run_job(client_brreg, job, today=TODAY)
    return job


def _names(job) -> list[str]:
    with zipfile.ZipFile(job.path) as zf:
        return sorted(zf.namelist())


def _report(job) -> str:
    with zipfile.ZipFile(job.path) as zf:
        return zf.read("MANGLER.txt").decode()


async def test_single_company_zip_is_flat_with_company_year_names(brreg, client_brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2022, 2023, 2024, 2025])
    job = await _run(client_brreg, ["998812623"], Selection("years", years=[2022, 2023, 2024]))
    assert (job.phase, job.filename, _names(job)) == (
        "done",
        "HJR_HOLDING_AS_2022-2024.zip",
        ["HJR_HOLDING_AS_2022.pdf", "HJR_HOLDING_AS_2023.pdf", "HJR_HOLDING_AS_2024.pdf"],
    )


async def test_zip_contents_are_the_pdfs(brreg, client_brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2024, 2025])
    job = await _run(client_brreg, ["998812623"], Selection("all"))
    with zipfile.ZipFile(job.path) as zf:
        assert zf.read("HJR_HOLDING_AS_2024.pdf") == b"%PDF-1.4 fake"


async def test_batch_zip_has_folder_per_company_and_report(brreg, client_brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2023, 2024, 2025])
    brreg.company("926052586", "ÆØÅ AS", [2024, 2025])
    brreg.underenhet("933158381", "HJR AS", parent="833155482")
    job = await _run(
        client_brreg,
        ["998812623", "926052586", "933158381", "927110423"],
        Selection("last", last_n=2),
        batch=True,
    )
    assert (job.phase, job.filename, _names(job)) == (
        "done",
        "årsregnskap_2_selskaper_2026-09-30.zip",
        [
            "HJR_HOLDING_AS_998812623/HJR_HOLDING_AS_2024.pdf",
            "HJR_HOLDING_AS_998812623/HJR_HOLDING_AS_2025.pdf",
            "MANGLER.txt",
            "ÆØÅ_AS_926052586/ÆØÅ_AS_2024.pdf",
            "ÆØÅ_AS_926052586/ÆØÅ_AS_2025.pdf",
        ],
    )
    assert _report(job) == (
        "Disse årsregnskapene kom ikke med:\n\n"
        "- HJR AS (933158381): Er en underenhet – årsregnskap ligger på hovedenheten (833155482)\n"
        "- 927110423: Finnes ikke i Enhetsregisteret\n"
    )


async def test_company_without_copies_is_reported(brreg, client_brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2024])
    brreg.company("980000176", "Eksempel Enk", [])
    job = await _run(client_brreg, ["998812623", "980000176"], Selection("all"), batch=True)
    assert [f.__dict__ for f in job.failures] == [
        {
            "orgnr": "980000176",
            "reason": "Brreg har ingen kopier av årsregnskap",
            "name": "Eksempel Enk",
            "year": None,
        }
    ]


async def test_pdf_404_mid_download_is_reported_not_silent(brreg, client_brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2023, 2024], missing_pdfs=(2023,))
    job = await _run(client_brreg, ["998812623"], Selection("all"))
    assert (job.phase, _names(job), job.companies[0].ok_years) == (
        "done",
        ["HJR_HOLDING_AS_2024.pdf", "MANGLER.txt"],
        [2024],
    )
    assert (
        "HJR HOLDING AS (998812623) 2023: Brreg har ingen kopi av årsregnskapet for 2023"
        in _report(job)
    )


async def test_requested_year_not_available_is_reported(brreg, client_brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2024])
    job = await _run(client_brreg, ["998812623"], Selection("years", years=[1999, 2024]))
    assert [(f.year, f.reason) for f in job.failures] == [(1999, "Ikke tilgjengelig hos Brreg")]


async def test_no_zip_without_report_when_everything_succeeds(brreg, client_brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2024, 2025])
    job = await _run(client_brreg, ["998812623"], Selection("all"))
    assert "MANGLER.txt" not in _names(job)


async def test_all_downloads_failing_marks_job_failed(brreg, client_brreg):
    brreg.company("998812623", "HJR HOLDING AS", [2024], missing_pdfs=(2024,))
    job = await _run(client_brreg, ["998812623"], Selection("all"))
    assert (job.phase, job.error, job.path) == (
        "failed",
        "Ingen av årsregnskapene kunne lastes ned",
        None,
    )


async def test_nothing_resolvable_marks_job_failed(brreg, client_brreg):
    job = await _run(client_brreg, ["998812623"], Selection("all"))
    assert (job.phase, job.error) == ("failed", "Fant ingen årsregnskap å laste ned")


@pytest.mark.parametrize("n_years,phase", [(299, "done"), (300, "done"), (301, "failed")])
async def test_max_files_cap(brreg, client_brreg, n_years, phase):
    brreg.company("998812623", "HJR HOLDING AS", list(range(1700, 1700 + n_years)))
    job = await _run(client_brreg, ["998812623"], Selection("all"))
    assert job.phase == phase


async def test_max_files_cap_downloads_nothing(brreg, client_brreg):
    brreg.company("998812623", "HJR HOLDING AS", list(range(1700, 2001)))
    job = await _run(client_brreg, ["998812623"], Selection("all"))
    assert (job.error, brreg.pdf_requests) == (
        "For mange filer (301). Maks 300 per nedlasting – velg færre år eller selskaper.",
        [],
    )


def test_cleanup_drops_expired_jobs(tmp_path):
    old = jobs.create_job("a@b.no", ["998812623"], Selection("all"), batch=False)
    old.path = tmp_path / "old.zip"
    old.path.write_bytes(b"x")
    fresh = jobs.create_job("a@b.no", ["998812623"], Selection("all"), batch=False)
    fresh.created = old.created + 1
    jobs.cleanup_jobs(now=old.created + 3600)
    assert (list(jobs.JOBS), old.path.exists()) == ([fresh.id], False)

"""Filenames for downloaded PDFs/ZIPs and the Content-Disposition header."""

import re
import unicodedata
from datetime import date
from urllib.parse import quote

MAX_NAME_LEN = 80
_ASCII_MAP = str.maketrans({"æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "å": "aa", "Å": "AA"})


def sanitize_name(name: str) -> str:
    """Company name → filesystem-safe token. Keeps æøå, spaces/slashes → `_`, drops the rest."""
    s = unicodedata.normalize("NFC", name or "").strip()
    s = re.sub(r"[\s/\\]+", "_", s)
    s = re.sub(r"[^\w\-]", "", s)
    s = re.sub(r"_{2,}", "_", s).strip("_-")
    s = s[:MAX_NAME_LEN].rstrip("_-")
    return s or "selskap"


def pdf_filename(company_name: str, year: int) -> str:
    """E.g. `HJR_HOLDING_AS_2024.pdf`."""
    return f"{sanitize_name(company_name)}_{year}.pdf"


def company_folder(company_name: str, orgnr: str) -> str:
    """Folder name inside a batch ZIP; orgnr disambiguates companies with equal names."""
    return f"{sanitize_name(company_name)}_{orgnr}"


def zip_filename(company_name: str, years: list[int]) -> str:
    """E.g. `HJR_HOLDING_AS_2012-2025.zip`; a single year gives `..._2024.zip`."""
    lo, hi = min(years), max(years)
    span = str(lo) if lo == hi else f"{lo}-{hi}"
    return f"{sanitize_name(company_name)}_{span}.zip"


def batch_zip_filename(n_companies: int, today: date) -> str:
    """E.g. `årsregnskap_3_selskaper_2026-09-30.zip`."""
    noun = "selskap" if n_companies == 1 else "selskaper"
    return f"årsregnskap_{n_companies}_{noun}_{today.isoformat()}.zip"


def _ascii_fallback(filename: str) -> str:
    s = unicodedata.normalize("NFKD", filename.translate(_ASCII_MAP))
    s = s.encode("ascii", "ignore").decode()
    return s.replace('"', "").replace("\\", "") or "download"


def content_disposition(filename: str) -> str:
    """RFC 6266 header value: ASCII `filename` fallback plus UTF-8 `filename*`."""
    return (
        f'attachment; filename="{_ascii_fallback(filename)}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )

"""Org number parsing/validation and year selection."""

import re
from dataclasses import dataclass
from typing import Literal

_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)
_ORGNR_IN_TEXT = re.compile(r"(?<!\d)(\d{3}[  ]?\d{3}[  ]?\d{3})(?!\d)")
_LIST_SEPARATORS = re.compile(r"[\n\r,;]+")


def has_valid_checksum(orgnr: str) -> bool:
    """MOD11 check digit used by Enhetsregisteret."""
    remainder = sum(int(d) * w for d, w in zip(orgnr[:8], _WEIGHTS, strict=True)) % 11
    check = (11 - remainder) % 11
    return check != 10 and check == int(orgnr[8])


def normalize_orgnr(text: str) -> str | None:
    """Return the 9 digits if `text` is an org number (whitespace allowed), else None."""
    digits = re.sub(r"\s+", "", text or "")
    if not re.fullmatch(r"\d{9}", digits):
        return None
    return digits


def orgnr_error(text: str) -> str | None:
    """Norwegian reason why `text` isn't a valid org number, or None if it is."""
    digits = re.sub(r"\s+", "", text or "")
    if not digits.isdigit():
        return "Ikke et organisasjonsnummer"
    if len(digits) != 9:
        return f"Organisasjonsnummer må ha 9 siffer (fikk {len(digits)})"
    if not has_valid_checksum(digits):
        return "Ugyldig organisasjonsnummer (feil kontrollsiffer)"
    return None


@dataclass
class ParsedList:
    orgnrs: list[str]
    invalid: list[tuple[str, str]]  # (raw input, reason)


def parse_orgnr_list(text: str) -> ParsedList:
    """Parse pasted org numbers: one per line or comma/semicolon separated. Deduplicates."""
    orgnrs: list[str] = []
    invalid: list[tuple[str, str]] = []
    for token in _LIST_SEPARATORS.split(text or ""):
        token = token.strip()
        if not token:
            continue
        found = _ORGNR_IN_TEXT.findall(token)
        if not found:
            invalid.append((token, orgnr_error(token) or "Ikke et organisasjonsnummer"))
            continue
        for raw in found:
            orgnr = re.sub(r"\D", "", raw)
            if error := orgnr_error(orgnr):
                invalid.append((raw, error))
            elif orgnr not in orgnrs:
                orgnrs.append(orgnr)
    return ParsedList(orgnrs=orgnrs, invalid=invalid)


SelectionMode = Literal["years", "all", "last", "range"]


def select_years(
    available: list[int],
    mode: SelectionMode,
    *,
    years: list[int] | None = None,
    last_n: int | None = None,
    from_year: int | None = None,
    to_year: int | None = None,
) -> list[int]:
    """Pick years out of `available` (sorted ascending result). Raises ValueError on bad input."""
    avail = sorted(set(available))
    if mode == "all":
        return avail
    if mode == "years":
        wanted = set(years or [])
        return [y for y in avail if y in wanted]
    if mode == "last":
        if last_n is None or last_n < 1:
            raise ValueError("Antall år må være minst 1")
        return avail[-last_n:]
    if mode == "range":
        if from_year is None or to_year is None:
            raise ValueError("Både fra- og til-år må fylles ut")
        if from_year > to_year:
            raise ValueError("Fra-år kan ikke være etter til-år")
        return [y for y in avail if from_year <= y <= to_year]
    raise ValueError(f"Ukjent utvalg: {mode}")

"""Search a company's read årsregnskap: running text (FTS5) and extracted figures, each hit with year and page."""

import re
from typing import Any

from src import db

# Match markers in snippets; the client escapes the text, then turns these into <mark>
HIT, END = "\x01", "\x02"
WORD = re.compile(r"[0-9a-zA-ZæøåÆØÅéèäöüÄÖÜ]+")
# Words too common in questions to say anything about which passage is meant
STOP = {
    "og", "i", "på", "til", "av", "for", "er", "det", "den", "de", "som", "med", "har", "hva", "hvor", "hvordan",
    "hvilke", "hvilken", "hvem", "når", "om", "en", "et", "ei", "var", "ble", "fra", "kan", "skal", "vil", "seg",
    "sin", "sitt", "sine", "selskapet", "selskapets", "noe", "nå", "år", "året", "the", "and", "of", "what", "is",
}  # fmt: skip
TEXT_LIMIT = 40
FIGURE_LIMIT = 40


def _terms(q: str, drop_stop: bool) -> list[str]:
    words = [w.lower() for w in WORD.findall(q)]
    return [w for w in words if not (drop_stop and (w in STOP or len(w) < 3))]


def _match(terms: list[str], joiner: str) -> str:
    """FTS5 query: each term quoted (no operator injection) and prefix-matched."""
    return f" {joiner} ".join(f'"{t}"*' for t in terms)


def text_hits(
    orgnr: str, q: str, any_word: bool = False, limit: int = TEXT_LIMIT
) -> list[dict[str, Any]]:
    """Passages of running text matching all words of `q` (or any word, for the chat), best first."""
    terms = _terms(q, drop_stop=any_word)
    if not terms:
        return []
    return db._all(
        f"SELECT year, page, note, kind, text, snippet(text_fts, 0, '{HIT}', '{END}', '…', 24) AS snippet "
        "FROM text_fts WHERE text_fts MATCH ? AND orgnr = ? ORDER BY bm25(text_fts) LIMIT ?",
        (_match(terms, "OR" if any_word else "AND"), orgnr, limit),
    )


def _amount(q: str) -> int | None:
    """'8 608 984', '-744099' or '8.608.984' as an integer; None when q isn't a number."""
    s = re.sub(r"[\s.  ]", "", q.strip()).replace("−", "-")
    return int(s) if re.fullmatch(r"-?\d{3,}", s) else None


def figure_hits(orgnr: str, q: str, limit: int = FIGURE_LIMIT) -> list[dict[str, Any]]:
    """Extracted values whose label holds every word of `q`, or whose amount is `q` (either sign)."""
    cols = "year, page, source, figures_for, copy, note, label, col, value, unit_scale"
    if (n := _amount(q)) is not None:
        return db._all(
            f"SELECT {cols} FROM figures WHERE orgnr = ? AND value IN (?, ?) ORDER BY year DESC, page LIMIT ?",
            (orgnr, n, -n, limit),
        )
    terms = _terms(q, drop_stop=False)
    if not terms:
        return []
    where = " AND ".join("lower(label) LIKE ?" for _ in terms)
    return db._all(
        f"SELECT {cols} FROM figures WHERE orgnr = ? AND {where} ORDER BY year DESC, page LIMIT ?",
        (orgnr, *[f"%{t}%" for t in terms], limit),
    )


def chat_excerpts(orgnr: str, question: str, limit: int = 12) -> str | None:
    """Text passages from the company's read filings that best match a chat question, as context lines."""
    hits = text_hits(orgnr, question, any_word=True, limit=limit)
    if not hits:
        return None
    lines = [
        "UTDRAG FRA ÅRSREGNSKAPENE (skrevet av fra PDF-ene; oppgi år og side når du bruker dem):"
    ]
    for h in hits:
        where = f"{h['year']} s. {h['page']}" + (f", note {h['note']}" if h["note"] else "")
        lines.append(f"- [{where}] {h['text']}")
    return "\n".join(lines)

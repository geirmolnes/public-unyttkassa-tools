"""Words the reader has muted for this browser session.

A running story floods the feed — PISA week, a strike — and the desk still has
to read the rest of it. A muted word hides every story that names it in the
title or ingress, for as long as the browser stays open, and nowhere else: it
lives in a session cookie, so it dies with the window and never reaches another
reader or another day. Nothing is written to the database.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote, unquote

from starlette.requests import Request
from starlette.responses import Response

COOKIE = "skjul"
MAX_TERMS = 8
MAX_CHARS = 40


def normalise(word: str | None) -> str | None:
    """One muted word as it is stored, or None when there is nothing to mute.

    Whitespace collapses and quotes go, because both would otherwise reach the
    FTS expression; a term with no word character at all is dropped rather than
    stored, since it can never match anything.
    """
    cleaned = re.sub(r"\s+", " ", (word or "").replace('"', "")).strip()[:MAX_CHARS].strip()
    return cleaned if re.search(r"\w", cleaned) else None


def read(request: Request) -> tuple[str, ...]:
    """The muted words in this request's cookie. Garbage reads as nothing."""
    raw = request.cookies.get(COOKIE)
    if not raw:
        return ()
    try:
        values = json.loads(unquote(raw))
    except (ValueError, TypeError):
        return ()
    if not isinstance(values, list):
        return ()
    return _dedupe(normalise(v) for v in values if isinstance(v, str))


def add(terms: tuple[str, ...], word: str | None) -> tuple[str, ...]:
    """`terms` with `word` appended. Past the cap the oldest word goes."""
    term = normalise(word)
    if term is None:
        return terms
    return _dedupe([*terms, term])[-MAX_TERMS:]


def remove(terms: tuple[str, ...], word: str | None) -> tuple[str, ...]:
    target = (normalise(word) or "").casefold()
    return tuple(t for t in terms if t.casefold() != target)


def write(response: Response, terms: tuple[str, ...], *, secure: bool) -> None:
    """Store `terms` in the session cookie; an empty list deletes it.

    No max-age on purpose: that is what makes it a session cookie. HttpOnly
    because no script needs it, and Lax so a link into the radar still carries
    it while a cross-site POST does not.
    """
    if not terms:
        response.delete_cookie(COOKIE, path="/")
        return
    response.set_cookie(
        COOKIE,
        quote(json.dumps(list(terms), ensure_ascii=False), safe=""),
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def _dedupe(values) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for v in values:
        if v and v.casefold() not in seen:
            seen[v.casefold()] = v
    return tuple(seen.values())[-MAX_TERMS:]

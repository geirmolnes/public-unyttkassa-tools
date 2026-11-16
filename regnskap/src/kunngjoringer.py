"""Kunngjøringer (public announcements) from Brønnøysundregistrene's search at w2.brreg.no.

There is no free API, so this reads the public HTML: one list page per company (id, date and
type of every announcement), then each announcement's own page once. Announcements never change,
so a fetched one is kept for good; only the list is fetched again (see db.KUNNGJORING_TTL).

Gently: every request from this process to w2.brreg.no goes through one pacer (PACE_SLOTS at a
time, at least PACE_GAP seconds apart), and one visit reads at most BATCH announcements, newest
first. The rest come on later visits, or one by one when opened.
"""

import asyncio
import html
import re
import time
from datetime import date
from typing import Any

BASE = "https://w2.brreg.no/kunngjoring"
PACE_SLOTS = 2
PACE_GAP = 0.25  # seconds between request starts: at most 4 a second, for every user together
BATCH = 12

# A notice about one company opens with its details as they stood then; kept apart from the rest.
IDENTITY = {
    "foretaksnavn", "navn/foretaksnavn", "navn", "organisasjonsnummer", "forretningsadresse",
    "adresse", "kommune", "organisasjonsform",
}  # fmt: skip
# Values that are running text wrapped at a fixed width, not lines (addresses, lists of people).
WRAPPED = {"formål", "vedtektsfestet formål"}
LIST_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
KID = re.compile(r"hent_en\.jsp\?kid=(\d+)")
DATE = re.compile(r"\b(\d\d)\.(\d\d)\.(\d{4})\b")
TITLE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S | re.I)
# Bold is a styled span, or <B> in notices from before about 2007.
BOLD = re.compile(r'<(?:span style="font-weight:bold;[^"]*"|b)>(.*?)</(?:span|b)>', re.S | re.I)
# The bold line closing each announcement: «Foretaksregisteret 05.10.2026».
FOOTER = re.compile(
    r'<(?:span style="font-weight:bold; "|b)>\s*([^<]*?)\s+(\d\d\.\d\d\.\d{4})\s*</(?:span|b)>',
    re.S | re.I,
)
# Where the notice starts: a document of its own, an <h3>, or (before about 2007) a bold title.
START = re.compile(r'<body|<h3|<b>|<span style="font-weight:bold', re.I)
PARAGRAPH = re.compile(r"(?:<br\s*/?>\s*){2,}", re.I)
NOT_REGISTERED = "ikke registrert i Enhetsregisteret"


def _text(fragment: str) -> str:
    """Tags out, entities decoded, runs of spaces folded (line breaks kept)."""
    t = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\xa0", " ")
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return "\n".join(line.strip() for line in t.split("\n") if line.strip())


def _iso(d: str) -> str:
    dd, mm, yyyy = DATE.search(d).groups()  # type: ignore[union-attr]
    return date(int(yyyy), int(mm), int(dd)).isoformat()


def parse_list(page: str) -> list[dict[str, str]] | None:
    """Every announcement on a company's list page, newest first: {kid, date, type}. A row
    without a date belongs to the dated row above it (one notice, several changes).
    None when the org.nr isn't registered at all."""
    if NOT_REGISTERED in page:
        return None
    out: list[dict[str, str]] = []
    for row in LIST_ROW.findall(page):
        kid = KID.search(row)
        if not kid:
            continue
        cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        when = next((c for c in cells if DATE.fullmatch(c)), None)
        kind = next((c for c in cells if c and not DATE.fullmatch(c)), "")
        if when is None and not out:
            continue
        out.append(
            {"kid": kid.group(1), "date": _iso(when) if when else out[-1]["date"], "type": kind}
        )
    return out


def _prose(piece: str) -> list[list[str]]:
    """Text outside the tables. Blank lines split paragraphs, and a single line break inside one
    is only the original's wrapping. A paragraph that is all bold is a heading; bold inside a
    sentence («Ved HORDALAND TINGRETT er det …») is just text."""
    out: list[list[str]] = []
    for para in PARAGRAPH.split(piece):
        t = _text(para).replace("\n", " ")
        if not t:
            continue
        bold = " ".join(_text(b) for b in BOLD.findall(para))
        out.append(["h" if bold == t else "p", t])
    return out


def _parts(body: str) -> list[list[str]]:
    """The notice in order: ["h", heading], ["p", paragraph] and ["f", label, value]. A table row
    without a label adds a line to the field above it (the next board member)."""
    parts: list[list[str]] = []
    depth = 0
    for piece in re.split(r"(</?table[^>]*>)", body, flags=re.I):
        if re.fullmatch(r"<table[^>]*>", piece, re.I):
            depth += 1
            continue
        if re.fullmatch(r"</table[^>]*>", piece, re.I):
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            parts += _prose(piece)
            continue
        marked = re.sub(r"<tr[^>]*>", "\x1e", piece, flags=re.I)
        marked = re.sub(r"</t[dh]>", "\x1f", marked, flags=re.I)
        for raw in marked.split("\x1e"):
            cells = [c for c in (_text(x) for x in raw.split("\x1f")) if c]
            if not cells:
                continue
            label = re.fullmatch(r"(.+?)\s*:", cells[0], re.S)
            if label:
                parts.append(["f", re.sub(r"\s+", " ", label.group(1)), "\n".join(cells[1:])])
            elif parts and parts[-1][0] == "f":
                parts[-1][2] = "\n".join(filter(None, [parts[-1][2], *cells]))
            else:
                parts.append(["p", " ".join(cells)])
    for part in parts:
        if part[0] == "f" and part[1].lower() in WRAPPED:
            part[2] = part[2].replace("\n", " ")
    return parts


def parse_one(page: str) -> dict[str, Any] | None:
    """One announcement's page as {title, register, date, name, address, parts, text}.

    `parts` (see _parts) is the notice minus its opening details about the company itself, which
    become `name` and `address` (as they stood then). That block is only taken when no heading
    comes before it: in a merger or demerger each company's block follows its role («Overtakende
    selskap») and all of them stay. `text` is the whole notice as plain text, for search."""
    # The notice is a fragment after the «Kunngjøring for: <org.nr>» header, sometimes a whole
    # HTML document of its own, sometimes just an <h3> and what follows.
    head = page.find("Kunngj&oslash;ring for")
    first = START.search(page, head) if head >= 0 else None
    if not first:
        return None
    start = first.start()
    end = page.find("hent_alle.jsp", start)
    feet = list(FOOTER.finditer(page, start, end if end > 0 else len(page)))
    if not feet:
        return None
    foot = feet[-1]
    body = page[start : foot.start()]
    title = TITLE.search(body)
    if title:
        body = body[title.end() :]
    parts = _parts(body)
    if not title:
        heading = next((i for i, p in enumerate(parts) if p[0] == "h"), None)
        if heading is None:
            return None
        title_text = parts.pop(heading)[1]
    else:
        title_text = _text(title.group(1))
    if title_text.isupper():  # «ENDRING AV STYRE» in older notices
        title_text = title_text.capitalize()
    who: dict[str, str] = {}
    first = _first_field(parts)
    if not any(p[0] == "h" for p in parts[:first]):
        i = first
        while (
            i < len(parts) and parts[i][0] == "f" and parts[i][1].lower() in IDENTITY - who.keys()
        ):
            who[parts[i][1].lower()] = parts[i][2]
            i += 1
        parts = parts[:first] + parts[i:]
    lines = [p[1] if p[0] != "f" else f"{p[1]}: {p[2]}" for p in parts]
    return {
        "title": title_text,
        "register": foot.group(1).strip(),
        "date": _iso(foot.group(2)),
        "name": who.get("foretaksnavn") or who.get("navn/foretaksnavn") or who.get("navn"),
        "address": (who.get("forretningsadresse") or who.get("adresse") or "").replace("\n", ", ")
        or None,
        "parts": parts,
        "text": "\n".join([title_text, *lines]),
    }


def _first_field(parts: list[list[str]]) -> int:
    return next((i for i, p in enumerate(parts) if p[0] == "f"), len(parts))


def list_url(orgnr: str) -> str:
    return f"{BASE}/hent_nr.jsp?orgnr={orgnr}"


def one_url(kid: str, orgnr: str) -> str:
    return f"{BASE}/hent_en.jsp?kid={kid}&sokeverdi={orgnr}&spraak=nb"


class Pacer:
    """At most `slots` requests at once, their starts at least `gap` seconds apart."""

    def __init__(self, slots: int, gap: float) -> None:
        self.loop = asyncio.get_running_loop()
        self.slots = asyncio.Semaphore(slots)
        self.turn = asyncio.Lock()
        self.gap = gap
        self.last = 0.0

    async def get(self, brreg: Any, url: str) -> str:
        async with self.slots:
            async with self.turn:
                wait = self.last + self.gap - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.last = time.monotonic()
            return await brreg.get_html(url)


_pacer: Pacer | None = None


def pacer() -> Pacer:
    """The process's pacer (made again for a new event loop, as in tests)."""
    global _pacer
    if _pacer is None or _pacer.loop is not asyncio.get_running_loop():
        _pacer = Pacer(PACE_SLOTS, PACE_GAP)
    return _pacer


async def fetch_list(brreg: Any, orgnr: str) -> list[dict[str, str]] | None:
    """The company's list page; None if Brreg doesn't know the org.nr."""
    return parse_list(await pacer().get(brreg, list_url(orgnr)))


async def fetch_details(brreg: Any, orgnr: str, kids: list[str]) -> dict[str, dict[str, Any]]:
    """These announcements read (through the pacer); one that won't parse maps to {}."""

    async def one(kid: str) -> tuple[str, dict[str, Any]]:
        return kid, parse_one(await pacer().get(brreg, one_url(kid, orgnr))) or {}

    return dict(await asyncio.gather(*(one(k) for k in kids)))

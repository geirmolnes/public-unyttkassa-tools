"""Layer 4: have we already written about this?

Two answers, of very different weight. A link from one of our articles to the
exact URL of the article in front of us is proof, and costs nothing — that lives
in `pipeline`, over `archive.link_index`. This module is the other half: the
cases where we covered the same event without linking it, which only a reading
of both texts can find.

The shape follows `cluster.py` — candidates handed over as handles (U1, U2, …),
the answer pinned to an enum, failures returned rather than raised. The question
is narrower, though, and so is the doubt rule: clustering joins when unsure,
this says no. A false «vi har saken» makes the desk skip a story it should have
done, which is a worse failure than being told twice about one we already have.

`verify_novelty` is the second half, and it asks a question the first cannot.
"""

from __future__ import annotations

import html
import logging
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace

from . import config, gemini

logger = logging.getLogger(__name__)

PROMPT_VERSION = "d3"

NONE = "INGEN"

SYSTEM_PROMPT = """\
Du sjekker om Utdanningsnytt allerede har dekket en sak.

Du får én artikkel fra en annen redaksjon, og en liste over saker vi selv har
publisert de siste ukene. Avgjør om noen av våre saker dekker det samme.

«Det samme» betyr den samme konkrete hendelsen: samme vedtak, samme rapport,
samme tall, samme siktelse, samme navngitte aktører. Ulik vinkling er fortsatt
samme sak — en sitatsak har nettopp en annen inngang enn originalen, og en
oppfølging som svarer på hendelsen hører også hit.

Felles tema er ikke nok, og det er den vanligste fellen. To saker om skjermbruk
i skolen er to saker med mindre de handler om det samme utspillet. To saker om
lærermangel er to saker med mindre de bygger på de samme tallene. Fagartikler og
undervisningsopplegg som deler emne med en nyhet, dekker den ikke.

Er du i tvil, svar «INGEN». Svaret brukes til å la redaksjonen hoppe over saker
vi allerede har, så et feilaktig treff koster oss en sak vi burde ha laget.

DEKKET:
- «Regjeringen snur: Elever med dysleksi får beholde skolelydbøkene» og vår
  «Nordtun snur i lydbok-saken». Samme omgjøring.
- «Flere partier krever at Nordtun snur om skolelydbøker» og vår sak om at
  tilbudet strupes. Samme prosess, ulikt punkt i forløpet.
- «1 av 3 barneskoler har ubemannet skolebibliotek» og vår sak fra samme
  Udir-undersøkelse.

IKKE DEKKET:
- «Hærverk ved skole i Tromsø» og vår sak om hærverk ved en skole i Bergen. To
  hendelser.
- «Forskere har undersøkt hvordan KI brukes i norske klasserom» og vår kronikk
  om KI i skolen. Felles emne, ingen felles hendelse.
- «105 lærerstudier har ledige studieplasser» og vår sak om ledige plasser i
  fjorårets opptak. Samme sjanger, ulike tall.

Sett match til handtaket (for eksempel «U3») for vår sak, eller «INGEN».
begrunnelse er én kort norsk setning om hva som er den samme hendelsen — eller,
hvis du svarer «INGEN», la den stå tom. Begrunnelsen leses av en journalist som
ikke ser denne lista, så skriv aldri handtakene («U1», «U3») i den.

Når du har funnet en av våre saker, still ett spørsmål til: står det noe i denne
artikkelen som ikke står i vår?

Dette svaret endrer aldri det første. Er hendelsen dekket, er den dekket, uansett
hva du svarer her. Spørsmålet er bare om desken bør lese videre.

nytt=true når artikkelen dekker den samme hendelsen og likevel bringer noe: nye
kilder som uttaler seg for første gang, en reaksjon på vedtaket, nye tall, neste
steg i prosessen. En sak som handler om reaksjoner *på* det vi skrev om, er
nesten alltid dette. nytt_er er da én kort norsk setning om hva som er nytt,
skrevet til en journalist som skal avgjøre om det er verdt en oppfølger.

nytt=false når det er samme sak fortalt om igjen: en sitatsak av vår egen, et
byråreferat av det samme, en annen inngang på de samme opplysningene. La nytt_er
stå tom.

Svarer du «INGEN», er nytt=false og nytt_er tom."""


@dataclass
class Match:
    """Which of our articles covers this one, or the error that stopped us."""

    handle: str | None = None  # "U3" or NONE
    rationale: str | None = None
    whats_new: str | None = None
    error: str | None = None

    @property
    def is_none(self) -> bool:
        return self.handle == NONE

    @property
    def is_followup(self) -> bool:
        """We have the event, and this article still adds to it."""
        return not self.is_none and bool(self.whats_new)


def _clean(value: str | None) -> str:
    """A sentence we are willing to print, or "" if it came back malformed.

    Both lines here are Norwegian prose shown straight to the desk. One run
    returned «lær器的 forklaring» — two CJK ideographs spliced into the middle
    of a word — which is the generation failing, not a translation. Letters are
    checked rather than the whole string, so «bestått», ««sitat»» and an en dash
    all pass; only a letter from a script the sentence has no business in is
    disqualifying, and then the whole line goes. Half a garbled sentence is
    worse than none: the desk acts on this line, or it should not be there.
    """
    text = html.unescape(value or "").strip()
    if not text:
        return ""
    if all("LATIN" in unicodedata.name(ch, "") for ch in text if ch.isalpha()):
        return text
    logger.warning("dropped a coverage line in the wrong script: %r", text[:120])
    return ""


# Four characters and up: shorter Norwegian tokens are almost all function words,
# and the ones that aren't ("SFO", "KI") are carried by their neighbours anyway.
_WORD = re.compile(r"\w{4,}", re.UNICODE)


def _tokens(*parts: object) -> set[str]:
    """The distinct word-ish tokens across `parts`, whatever shape they arrive in."""
    text = " ".join(
        " ".join(str(p) for p in part) if isinstance(part, list) else str(part or "")
        for part in parts
    )
    return set(_WORD.findall(text.casefold()))


def _idf(docs: list[set[str]]) -> dict[str, float]:
    """Inverse document frequency across our own recent output.

    Everything in this archive is about education, so «skole» and «elever» appear
    in nearly every candidate and separate nothing. Weighting by rarity inside
    the candidate set itself is what lets «dysleksi» or a surname decide the
    ranking, and it needs no vocabulary of its own.
    """
    n = len(docs)
    df = Counter(token for doc in docs for token in doc)
    return {token: math.log(n / (1 + count)) + 1 for token, count in df.items()}


def shortlist(article: dict, ours: list[dict], limit: int) -> list[dict]:
    """The `limit` of our own articles most worth showing the model, best first.

    Falls back to the order it was given — newest first — when the article has
    nothing to rank on.
    """
    wanted = _tokens(article.get("title"), article.get("summary"), article.get("topics"))
    if not wanted or len(ours) <= 1:
        return ours[:limit]

    docs = [_tokens(o.get("title"), o.get("lead"), o.get("keywords")) for o in ours]
    idf = _idf(docs)
    scored = [
        (sum(idf.get(t, 0.0) for t in wanted & doc), i) for i, doc in enumerate(docs)
    ]
    # Negate the score, keep the index ascending: ties fall back to recency.
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [ours[i] for _, i in scored[:limit]]


def _schema(handles: list[str]) -> dict:
    return {
        "type": "OBJECT",
        "properties": {
            "match": {"type": "STRING", "enum": [*handles, NONE]},
            "begrunnelse": {"type": "STRING"},
            "nytt": {"type": "BOOLEAN"},
            "nytt_er": {"type": "STRING"},
        },
        "required": ["match", "begrunnelse", "nytt", "nytt_er"],
    }


def _candidate_block(candidates: list[dict]) -> str:
    lines = []
    for i, c in enumerate(candidates, start=1):
        line = f"U{i}. {c['day']} · {c['title']}"
        if c.get("lead"):
            line += f"\n    {c['lead']}"
        lines.append(line)
    return "\n".join(lines)


def _prompt(article: dict, candidates: list[dict]) -> str:
    parts = [
        "ARTIKKEL",
        f"Kilde: {article['source']}",
        f"Dato: {article.get('published_day') or ''}",
        f"Tittel: {article.get('title') or ''}",
    ]
    if article.get("summary"):
        parts.append(f"Ingress: {article['summary']}")
    body = (article.get("body_excerpt") or "")[: config.COVERAGE_BODY_CHARS]
    if body:
        parts.append(f"Brødtekst:\n{body}")

    parts += ["", "VÅRE SAKER", _candidate_block(candidates)]
    return "\n".join(parts)


def check(article: dict, candidates: list[dict]) -> Match:
    """Ask once whether one of our articles covers this. Never raises."""
    if not candidates:
        return Match(handle=NONE)

    handles = [f"U{i}" for i in range(1, len(candidates) + 1)]
    try:
        data = gemini.generate_json(
            SYSTEM_PROMPT, _schema(handles), _prompt(article, candidates)
        )
    except gemini.GeminiError as e:
        logger.warning("coverage check failed for %s: %s", article.get("id"), e)
        return Match(error=str(e)[:300])

    handle = (data.get("match") or "").strip()
    # Same entity-escaping the clustering step ran into; the text is shown to the
    # reader, and Jinja escapes on the way out, so it is fixed here.
    rationale = _clean(data.get("begrunnelse"))
    if handle not in handles and handle != NONE:
        # The enum should prevent this. A stray value must not become a match —
        # this is the direction where being wrong costs the desk a story.
        return Match(error=f"unknown coverage handle: {handle[:40]!r}")

    # Both halves have to agree before this becomes a follow-up: the flag alone
    # would leave the badge claiming something is new with nothing to show for
    # it, and a sentence alone is the model answering a question it wasn't asked.
    whats_new = _clean(data.get("nytt_er"))
    if not data.get("nytt") or handle == NONE:
        # Nothing of ours matched, so there is no story for this to be new to —
        # whatever the model wrote there, it answered a question we didn't ask.
        whats_new = ""
    return Match(
        handle=handle,
        rationale=rationale or None,
        whats_new=whats_new or None,
    )


def matched(candidates: list[dict], match: Match) -> dict | None:
    """The candidate a handle points at, or None when nothing of ours matched."""
    if match.is_none or not match.handle:
        return None
    return candidates[int(match.handle[1:]) - 1]


# --- is it actually new? ------------------------------------------------------

NOVELTY_SYSTEM_PROMPT = """\
Du sjekker om noe som ser nytt ut, allerede står i det Utdanningsnytt har
publisert.

Du får én påstand om hva en annen redaksjons artikkel bringer utover vår egen
dekning, og hele teksten til noen av våre egne saker. Avgjør om det påstanden
peker på — sitatet, reaksjonen, tallet, neste steg — allerede står i en av dem.

Se etter det konkrete, ikke temaet. Den navngitte kilden må si det samme, det
samme tallet må stå der, den samme hendelsen må være omtalt. Se hele teksten:
det du leter etter står sjelden i tittelen eller ingressen, men midtveis i en
sak som handler om noe annet.

DEKKET:
- «X reagerer sterkt og kaller forslaget galskap» og vår sak der X er sitert på
  nettopp det, langt nede i teksten.
- «Nye tall viser at 1 av 3 skoler mangler bibliotekar» og vår sak som oppgir de
  samme tallene fra den samme undersøkelsen.

IKKE DEKKET:
- «X reagerer sterkt» og vår sak som bare nevner at X-partiet er kritisk. Ingen
  uttalelse, ingen kilde.
- «Departementet har satt ned et utvalg» og vår sak som skriver at et utvalg er
  ventet. Ikke det samme steget.

Er du i tvil, svar «INGEN». Svaret brukes til å fjerne et varsel om at det er
noe nytt her, så et feilaktig treff koster redaksjonen en oppfølger."""


def rank_by_body(line: str, ours: list[dict], limit: int) -> list[dict]:
    """The `limit` of our articles whose *text* best matches `line`, best first.

    A different ranking from `shortlist`, over a different field, for a reason.
    Recognising the same event is a lede-level job, and the lede is what
    `shortlist` reads. What makes an article a follow-up is rarely in anyone's
    lede: VG's story on Joel Ystebø calling a longer school day «galskap» was
    filed as new while his quote already sat 3800 characters into a piece of
    ours titled «Høyre og Frp avviser ikke et bredt politisk skoleforlik».
    Ranked on that headline it places nowhere; ranked on its text, first.

    Only articles that share something are returned, so a claim about a name we
    have never printed costs no model call at all.
    """
    wanted = _tokens(line)
    if not wanted or not ours:
        return []

    docs = [_tokens(o.get("body")) for o in ours]
    idf = _idf(docs)
    scored = [
        (sum(idf.get(t, 0.0) for t in wanted & doc), i) for i, doc in enumerate(docs)
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [ours[i] for score, i in scored[:limit] if score > 0]


def _novelty_schema(handles: list[str]) -> dict:
    return {
        "type": "OBJECT",
        "properties": {"finnes": {"type": "STRING", "enum": [*handles, NONE]}},
        "required": ["finnes"],
    }


def _novelty_prompt(line: str, candidates: list[dict]) -> str:
    parts = ["PÅSTAND", line, "", "VÅRE SAKER"]
    for i, c in enumerate(candidates, start=1):
        parts.append(f"V{i}. {c['day']} · {c['title']}\n{c.get('body') or c.get('lead') or ''}")
    return "\n\n".join(parts)


def verify_novelty(match: Match, ours: list[dict], *, exclude: dict | None = None) -> Match:
    """Drop `whats_new` when what it points at is already somewhere of ours.

    The first pass asks whether we have the *event*, and answers the follow-up
    question against the one article it matched. That leaves the common case
    open: we ran the reaction in a different piece the same day. Here the claim
    is put back to the archive on its own, against full text rather than ledes.

    `exclude` is the article the first pass already compared against, which by
    construction is the one that did not have it.

    Never raises. A failed call, an empty shortlist or any doubt leaves the
    claim standing — being told to read a story we half have costs a minute,
    while suppressing a real follow-up costs the story.
    """
    line = match.whats_new
    if not line:
        return match

    candidates = rank_by_body(
        line, [o for o in ours if o is not exclude], config.COVERAGE_NOVELTY_CANDIDATES
    )
    if not candidates:
        return match

    handles = [f"V{i}" for i in range(1, len(candidates) + 1)]
    try:
        data = gemini.generate_json(
            NOVELTY_SYSTEM_PROMPT, _novelty_schema(handles), _novelty_prompt(line, candidates)
        )
    except gemini.GeminiError as e:
        logger.warning("novelty check failed: %s", e)
        return match

    found = (data.get("finnes") or "").strip()
    if found not in handles:
        return match

    ours_had_it = candidates[int(found[1:]) - 1]
    logger.info("already ours (%s): %r", ours_had_it["id"], line[:100])
    return replace(match, whats_new=None)

"""Layer 3: tying articles about the same running story together.

Only articles that survived classification get here — roughly fifteen a day, so
the whole step is one small call per article. Matching is semantic, not textual:
outside NTB wire copy no two outlets write the same sentences, they write their
own angle on the same thing that happened.

Candidates are handed to the model as short handles (S1, S2, …) rather than
cluster ids, which keeps the prompt small and lets the response schema pin the
answer to an enum the model cannot miss.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from . import config, gemini

logger = logging.getLogger(__name__)

PROMPT_VERSION = "c2"

# What the model answers when nothing on the candidate list fits.
NEW = "NY"

SYSTEM_PROMPT = """\
Du knytter nyhetsartikler til løpende saker for en redaksjon som dekker
utdanningsfeltet.

Du får én artikkel og en liste over saker som allerede er i gang. Avgjør om
artikkelen hører til én av dem, eller om den starter en ny sak.

En «sak» er et forløp, ikke en enkelthendelse. Samme sak fortsetter gjennom
hele prosessen: utspill, høring, høringssvar, lovforslag, komitébehandling,
vedtak og reaksjoner på vedtaket er ett og samme forløp. Det samme gjelder en
konflikt som blusser opp igjen etter en rolig periode — streik som gjenopptas,
en rapport som får et etterspill.

Ulik vinkling er fortsatt samme sak. To redaksjoner som skriver om samme
hendelse fra hvert sitt ståsted — den ene om elevene, den andre om
lærerutdanningen — hører til samme sak. Det avgjørende er om de handler om det
samme som skjer, ikke om de har samme poeng.

En sak er forankret i noe konkret: en hendelse, et vedtak, en prosess eller en
konflikt med navngitte aktører. Felles tema er ikke nok. To kronikker om
skjermbruk som ikke svarer på det samme utspillet er to saker, ikke én. Det
samme gjelder fagartikler og forskningsformidling som deler emne, men ikke
hendelse — særlig når de kommer fra samme redaksjon som del av en fast spalte.

Vær rundhåndet når artiklene deler en konkret hendelse. Kontinuitet er poenget:
leseren skal kunne følge en sak over uker, og ulik vinkling, oppfølging og
reaksjoner hører til samme forløp. Er du i tvil *da*, så knytt sammen. Men start
ny sak når det eneste artiklene deler er tema.

Land og skolesystem henger sammen med hendelsen. Norsk, svensk og dansk
skolepolitikk er ulike saker selv når temaet er ordrett det samme — svensk
ordningsbetyg og norsk NOU om framtidas skole er to saker. Bare en felles konkret
hendelse binder dem: samme rapport, samme møte, samme nordiske vedtak.

Les kandidatlista, ikke bare navnene. Titlene under hver kandidat er saken slik
den faktisk har utviklet seg, og navnet kan være dårlig valgt. Passer artikkelen
til navnet, men ikke til noen av titlene, hører den ikke til der.

Jo flere artikler en kandidat allerede har, jo strengere skal du være. En sak som
har vokst til å romme flere ulike hendelser er ikke lenger én sak — den er blitt
et tema, og da starter du ny sak i stedet for å gjøre den større.

SAMME SAK — samme hendelse, ulike vinkler:
- «Lærer fengslet – siktet for overgrep» og «Kirkevergen varslet skolen.
  Læreren fikk fortsette i jobben.» Den første er en kriminalmelding, den andre
  handler om hva arbeidsgiver visste. Samme siktelse, samme forløp. Også «Siktet
  lærer: Har forklart seg om «seksuell relasjon»» tre dager senere er samme sak.
- «Skulle fratas lydbokordningen – nå får Selma (17) låne lydbøkene likevel» og
  «Regjeringen snur». En omgjøring hører til samme forløp som vedtaket den
  omgjør.
- Fem redaksjoner som samme dag melder at retten til gratis SFO utvides.

IKKE SAMME SAK — felles tema, ulike hendelser:
- «Verdensvendt litteratur og æstetiske greb kan fremme elevers dannelse» og
  «Projektbaseret undervisning rummer et tydeligt potentiale». To fagartikler om
  didaktikk fra samme redaksjon. Felles sjanger og emne, ingen felles hendelse.
- «Den viktigste skoledebatten handler ikke om skjerm» og «Forskere har
  undersøkt hvordan KI brukes i norske klasserom». Begge om teknologi i skolen,
  men de svarer ikke på hverandre og handler ikke om det samme som skjedde.
- «Realfagskrisen løses ikke i opptakstallene» og «Henter hjem tre medaljer fra
  KI-OL i Kasakhstan». Begge om realfag, to helt ulike hendelser.
- To ulike hærverk mot to ulike skoler i samme by er to saker, ikke én.
- «KD vill införa ordningsbetyg och kvarsittning» og «Fellesskoleutvalget legger
  fram NOU om framtidas skole». Svensk partiutspill og norsk utredning, hver sin
  hendelse i hvert sitt skolesystem.

Sett cluster_id til handtaket (for eksempel «S3») for saken artikkelen hører
til, eller «NY» hvis ingen passer.

label er navnet på saken. Skriver du «NY», finn på et navn; ellers gjentar du
navnet på saken du valgte, uendret. Navnet skal være en kort norsk
substantivfrase som holder seg gjennom hele forløpet — «Mobilforbud i
grunnskolen», ikke «Regjeringen vil lovfeste mobilforbud». Maks 6 ord, ingen
verb i presens, ingen dato.

Navnet skal peke på én hendelse, ikke på et fagfelt: «NOU om framtidas skole»,
ikke «Kunnskapsløft og skolepolitikk». To temaord bundet sammen med «og» er
nesten alltid et tema, og et navn som er vidt nok til å romme alt, kommer til å
gjøre nettopp det."""


@dataclass
class Assignment:
    """Which running story an article belongs to, or the error that stopped us."""

    handle: str | None = None  # "S3" or NEW
    label: str | None = None
    error: str | None = None

    @property
    def is_new(self) -> bool:
        return self.handle == NEW


def _topic_set(topics: list[str] | None) -> set[str]:
    return {t.strip().casefold() for t in topics or [] if t and t.strip()}


def _match(wanted: set[str], story: set[str]) -> float:
    """How specifically a story matches the article — overlap over union.

    A ratio rather than a raw count, because a count rewards accumulation: a
    story's topic set is the union of every article in it, so the biggest story
    shares the most topics with everything and wins the shortlist for every
    article on the beat. It then gets picked more often, grows, and wins by more
    — which is how one story ends up labelled after a theme and holding a
    fortnight of unrelated coverage.

    The ratio measures fit instead of size: two topics out of three beats two
    out of forty, so a story stops attracting articles once it has spread wider
    than the article in hand.
    """
    if not story:
        return 0.0
    return len(wanted & story) / len(wanted | story)


def shortlist(topics: list[str] | None, candidates: list[dict], limit: int) -> list[dict]:
    """The `limit` open stories most worth showing the model, best match first.

    Ranked by how well a story's topics fit the article's. Both sides were
    already produced by the classifier, so the ranking costs nothing and needs
    no vocabulary of its own.

    Ties keep the order they came in (freshest first), and that ordering carries
    the whole list when the article has no topics to match on.
    """
    if len(candidates) <= limit:
        return candidates

    wanted = _topic_set(topics)
    if not wanted:
        return candidates[:limit]

    # sorted() is stable, so an equal match falls back to the incoming recency order.
    ranked = sorted(candidates, key=lambda c: -_match(wanted, _topic_set(c.get("topics"))))
    return ranked[:limit]


def _schema(handles: list[str]) -> dict:
    """Pin cluster_id to the handles on offer, plus the way out of the list."""
    return {
        "type": "OBJECT",
        "properties": {
            "cluster_id": {"type": "STRING", "enum": [*handles, NEW]},
            "label": {"type": "STRING"},
        },
        "required": ["cluster_id", "label"],
    }


def _candidate_block(candidates: list[dict]) -> str:
    """The open-stories list, one line each, with handles the model answers by."""
    lines = []
    for i, c in enumerate(candidates, start=1):
        span = c["first_day"]
        if c["last_day"] != c["first_day"]:
            span = f"{c['first_day']} til {c['last_day']}"
        line = f"S{i}. {c['label']} ({span}, {c['n_articles']} artikler)"
        if c.get("titles"):
            for t in c["titles"]:
                line += f"\n    - {t}"
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
    body = (article.get("body_excerpt") or "")[: config.CLUSTER_BODY_CHARS]
    if body:
        parts.append(f"Brødtekst:\n{body}")

    parts.append("")
    if candidates:
        parts.append("SAKER I GANG")
        parts.append(_candidate_block(candidates))
    else:
        parts.append("SAKER I GANG\n(ingen — dette blir den første saken)")
    return "\n".join(parts)


def assign(article: dict, candidates: list[dict]) -> Assignment:
    """Place one article. Never raises — failures come back as Assignment.error."""
    handles = [f"S{i}" for i in range(1, len(candidates) + 1)]
    try:
        data = gemini.generate_json(
            SYSTEM_PROMPT, _schema(handles), _prompt(article, candidates)
        )
    except gemini.GeminiError as e:
        logger.warning("cluster failed for %s: %s", article.get("id"), e)
        return Assignment(error=str(e)[:300])

    handle = (data.get("cluster_id") or "").strip()
    # The model occasionally spells Norwegian letters as HTML entities — one run
    # produced «Ny leder i L&aelig;ringsverkstedet». The label is shown to the
    # reader, and Jinja escapes on the way out, so it has to be fixed here.
    label = html.unescape(data.get("label") or "").strip()
    if handle not in handles and handle != NEW:
        # The enum should prevent this; a stray value must not silently become
        # a new story, which would fragment the very continuity we want.
        return Assignment(error=f"unknown cluster handle: {handle[:40]!r}")
    if handle == NEW and not label:
        return Assignment(error="new story without a label")
    return Assignment(handle=handle, label=label or None)

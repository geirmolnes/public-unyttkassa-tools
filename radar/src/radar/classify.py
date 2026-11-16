"""Layer 2: LLM adjudication and scoring.

The lexical gate answers "does this mention school at all". This answers the
harder question — is the article *about* education, and how much would a
journalist on the beat care.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config, gemini

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v4"

KINDS = ["nyhet", "debatt", "forskning", "fagstoff"]

STAGES = [
    "barnehage",
    "grunnskole",
    "videregaende",
    "laererutdanning",
    "tverrgaende",
    "ikke_relevant",
]

SYSTEM_PROMPT = """\
Du vurderer nyhetsartikler for en redaksjon som dekker utdanningsfeltet i Norden.

Nedslagsfeltet er barnehage, SFO, grunnskole og videregående opplæring — pluss
lærerprofesjonen og lærerutdanningen. Leserne er lærere, barnehagelærere,
skoleledere, skoleeiere og utdanningspolitikere.

RELEVANT er saker om:
- skole- og barnehagepolitikk, lovverk, læreplaner, finansiering og eierskap
- arbeidsvilkår, lønn, streik, bemanning og rekruttering i skole og barnehage
- undervisning, læringsmiljø, mobbing, spesialundervisning, tilpasset opplæring
- elevers resultater, prøver, eksamen, fravær og gjennomføring
- forskning på barnehage, skole og lærerprofesjonen
- lærerutdanning, praksis, etter- og videreutdanning for lærere
- yrkesfag, lærlinger og fagopplæring
- saker der en skole eller barnehage er den handlende parten, ikke bare kulissen
- kriminal-, tilsyns- og rettssaker der en lærer, barnehageansatt eller
  skoleleder er part i kraft av rollen sin: overgrep, vold, varsling, gransking
  og oppsigelse som gjelder yrkesutøvelsen eller arbeidsgivers håndtering. Hele
  forløpet er relevant, også de første meldingene om pågripelse og siktelse —
  ikke bare oppfølgingen om hva skolen visste

IKKE RELEVANT:
- generelle nyheter der skole eller barn bare nevnes i forbifarten
- kriminal- og ulykkessaker der skolen bare er åsted og verken skolen, de
  ansatte eller opplæringen er part — et trafikkuhell utenfor skoleporten
- barne- og ungdomsstoff uten kobling til barnehage, skole eller opplæring
- sport, kultur og kjendisstoff der en person tilfeldigvis er lærer eller elev

Vær særlig streng med høyere utdanning. Universitet og høgskole er utenfor
feltet selv når saken handler om studenter, studietilbud, opptak, akkreditering
og Nokut, campus, studentvelferd eller institusjonsøkonomi. Slike saker er bare
relevante hvis de handler om lærerutdanningen, eller om hvordan barnehage og
skole påvirkes. Flere av kildene (blant andre Khrono) dekker mest høyere
utdanning — de fleste sakene derfra skal falle ut.

kind sier hva slags tekst det er:
- nyhet: noe har skjedd. Navngitte aktører, vedtak, tall, konflikt, hendelse.
- debatt: innlegg, kronikk, leder, kommentar — en mening, ikke en hendelse.
- forskning: studie, rapport, avhandling eller forskningsformidling.
- fagstoff: didaktikk, undervisningsopplegg, metode, portretter av praksis,
  bacheloroppgaver og prisnominasjoner. Tidløst stoff uten nyhetskrok.

score er 0-100 og sier hvor godt saken treffer *nyhetsarbeidet* i redaksjonen.
Nedslagsfeltet avgjør om saken er relevant i det hele tatt; scoren avgjør hvor
høyt den skal ligge. En viktig bruk er å finne saker det kan lages sitatsak av —
altså saker med navngitte kilder, konkrete påstander og sitater som kan siteres
videre.

- 80-100: nyhet med tydelig krok — vedtak, konflikt, nye tall, navngitte kilder
  som sier noe det går an å sitere
- 60-79: klar nyhet, men mindre eller mer lokal
- 40-59: forskning og fagstoff med en nyhetskrok, eller debatt fra en tung stemme
- 20-39: rent fagstoff og didaktikk uten nyhetsverdi. Relevant lesning for
  leserne, men ingen sak å gå videre på. Bacheloroppgaver, prisnominasjoner,
  undervisningsopplegg og metodeartikler hører nesten alltid hjemme her.
- 1-19: så vidt innom feltet
- 0: ikke relevant i det hele tatt

Godt fagstoff skal altså ligge under en middels nyhet, uansett hvor godt det
treffer temaet. relevant=true så lenge saken hører til feltet, også for fagstoff.

Svar bare med JSON etter det oppgitte skjemaet. rationale skal være på norsk og
maks 15 ord."""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "relevant": {"type": "BOOLEAN"},
        "score": {"type": "INTEGER"},
        "stage": {"type": "STRING", "enum": STAGES},
        "kind": {"type": "STRING", "enum": KINDS},
        "topics": {"type": "ARRAY", "items": {"type": "STRING"}},
        "rationale": {"type": "STRING"},
    },
    "required": ["relevant", "score", "stage", "kind", "topics", "rationale"],
}


@dataclass
class Verdict:
    """One article's classification, or the error that prevented it."""

    relevant: bool | None = None
    score: int | None = None
    stage: str | None = None
    kind: str | None = None
    topics: list[str] | None = None
    rationale: str | None = None
    error: str | None = None


def _prompt(article: dict) -> str:
    parts = [
        f"Kilde: {article['source']}",
        f"Tittel: {article.get('title') or ''}",
    ]
    if article.get("summary"):
        parts.append(f"Ingress: {article['summary']}")
    body = (article.get("body_excerpt") or "")[: config.CLASSIFY_BODY_CHARS]
    if body:
        parts.append(f"Brødtekst:\n{body}")
    return "\n".join(parts)


def classify(article: dict) -> Verdict:
    """Score one article. Never raises — failures come back as Verdict.error."""
    try:
        data = gemini.generate_json(SYSTEM_PROMPT, RESPONSE_SCHEMA, _prompt(article))
    except gemini.GeminiError as e:
        logger.warning("classify failed for %s: %s", article.get("id"), e)
        return Verdict(error=str(e)[:300])

    relevant = bool(data.get("relevant"))
    return Verdict(
        relevant=relevant,
        score=_score(data.get("score"), relevant=relevant),
        stage=_one_of(data.get("stage"), STAGES, "tverrgaende" if relevant else None),
        kind=_one_of(data.get("kind"), KINDS, "nyhet" if relevant else None),
        topics=[str(t) for t in (data.get("topics") or []) if str(t).strip()],
        rationale=(data.get("rationale") or "").strip() or None,
    )


def _score(value: object, *, relevant: bool) -> int | None:
    """Clamp the model's score, or fall back to the bottom of the scale.

    A NULL score on a relevant article is worse than a wrong one: every feed
    query filters on `v.score >= ?`, and SQL comparisons against NULL are
    neither true nor false, so the row disappears from the feed *and* from every
    count with no error anywhere. 0 keeps it filed and findable at «Alt».
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0 if relevant else None
    return max(0, min(100, int(value)))


def _one_of(value: object, allowed: list[str], fallback: str | None) -> str | None:
    """Keep an enum value the display layer has a label for.

    The response schema pins these, but a schema is a request rather than a
    guarantee, and `ikke_relevant` is in STAGES while STAGE_LABELS has no entry
    for it — so a relevant article classified that way would render its raw key
    in the rail and be counted under a heading nobody can click.
    """
    if isinstance(value, str) and value in allowed and value != "ikke_relevant":
        return value
    return fallback

"""LLM-backed verification for quotes that fail literal source matching.

Handles quotes translated into the article's målform (Bokmål or Nynorsk)
from other languages, where byte-for-byte matching against sources will
not work.
"""

import logging

from pydantic import BaseModel
from pydantic_ai import Agent, PromptedOutput

from . import run_usage
from .metered import metered
from .models import (
    ANTHROPIC_SETTINGS,
    OPENROUTER_SETTINGS,
    PRIMARY,
    RECOVERABLE_ERRORS,
    THINKING_LEVEL,
    retry_model,
    run_settings,
)

log = logging.getLogger(__name__)


def _system(*, nynorsk: bool = False) -> str:
    """Verifier instructions targeting the article's målform."""
    if nynorsk:
        malform = "nynorsk"
        andre = "dansk, svensk eller engelsk"
        andre_korrigert = "danske, svenske eller bokmålske"
        sier = "seier"
    else:
        malform = "bokmål"
        andre = "dansk, nynorsk, svensk eller engelsk"
        andre_korrigert = "danske, nynorske eller svenske"
        sier = "sier"
    return (
        f"Du verifiserer sitater i en norsk ({malform}) nyhetsartikkel mot "
        "kildemateriale. Kildene kan være på nynorsk, dansk, svensk, engelsk "
        "eller andre språk.\n\n"
        f"Artikkelen er på {malform}, og sitater MÅ være på korrekt {malform}. Et "
        f"sitat på {andre} er ikke akseptabelt "
        f"selv når innholdet stemmer — det må oversettes til naturlig {malform}.\n\n"
        "For hvert sitat:\n"
        f"• ok=true kun når (a) sitatet allerede er korrekt {malform} OG (b) en "
        "passasje i kildene uttrykker samme innhold (samme taler, samme "
        "betydning, ingen tilleggspåstander). Gjengi kildepassasjen ordrett "
        "(på originalspråket) i source_snippet. La corrected_quote være null.\n"
        "• ok=false ellers. Hvis kilden har en relatert passasje, skriv en "
        "korrigert versjon av hele sitat-linjen i corrected_quote — oversett "
        f"innholdet til naturlig {malform} og bevar attributtering "
        f"(«, {sier} NN til X.»). Hvis ingen relatert passasje finnes, la "
        "corrected_quote være null.\n\n"
        "Noen sitater er korte spenn hentet fra løpende tekst og mangler "
        "attribusjon — for disse skal corrected_quote kun inneholde selve "
        "sitatteksten, uten anførselstegn og uten å legge til attribusjon.\n\n"
        f"Korrigerte sitater må selv være på korrekt {malform} — ikke gjengi "
        f"{andre_korrigert} former i corrected_quote. Begrunn "
        "kort i reason."
    )


class QuoteVerdict(BaseModel):
    quote: str
    ok: bool
    source_snippet: str | None
    corrected_quote: str | None
    reason: str


class VerifyResponse(BaseModel):
    verdicts: list[QuoteVerdict]


_agents = {
    nynorsk: Agent(
        metered(PRIMARY),
        instructions=_system(nynorsk=nynorsk),
        output_type=VerifyResponse,
        model_settings={
            "google_thinking_config": {"thinking_level": THINKING_LEVEL},
            **OPENROUTER_SETTINGS,
            **ANTHROPIC_SETTINGS,
        },
    )
    for nynorsk in (False, True)
}


def _output_type(model: str):
    """Structured output mode for `model`.

    pydantic-ai's default forces a tool call, which Sonnet 5.5 rejects with
    a 400. Schema-in-prompt JSON works on every fallback.
    """
    return VerifyResponse if model.startswith("google-gla:") else PromptedOutput(VerifyResponse)


async def verify_quotes(
    quotes: list[str],
    source_texts: list[str],
    forced_translation: list[bool] | None = None,
    *,
    nynorsk: bool = False,
) -> list[QuoteVerdict]:
    """Ask the LLM to verify each quote against the sources.

    `forced_translation[i] = True` tells the LLM that quote `i` has already
    been confirmed as non-Bokmål by upstream checks (regex marker hit, .dk/
    .se source). For those, ok=True is not a valid response and the LLM
    must produce a Bokmål corrected_quote.
    """
    if not quotes:
        return []

    if forced_translation is None:
        forced_translation = [False] * len(quotes)

    sources_block = "\n\n".join(
        f"=== KILDE {i + 1} ===\n{t}" for i, t in enumerate(source_texts) if t
    )

    malform = "NYNORSK" if nynorsk else "BOKMÅL"
    andre = "dansk eller svensk" if nynorsk else "dansk, nynorsk eller svensk"

    def _label(i: int, q: str, forced: bool) -> str:
        tag = f" [BEKREFTET IKKE {malform} — MÅ OVERSETTES]" if forced else ""
        return f"{i + 1}.{tag} {q}"

    quotes_block = "\n".join(
        _label(i, q, f) for i, (q, f) in enumerate(zip(quotes, forced_translation))
    )

    forced_note = (
        f"\n\nVIKTIG: Sitater merket [BEKREFTET IKKE {malform} — MÅ OVERSETTES] er "
        f"allerede bekreftet å være på {andre} av en "
        "uavhengig sjekk. For disse må du ALLTID returnere ok=false og "
        f"produsere en {malform.lower()}-oversettelse i corrected_quote. ok=true er ikke "
        "en gyldig respons for disse."
        if any(forced_translation)
        else ""
    )

    user_prompt = (
        f"KILDEMATERIALE:\n{sources_block}\n\n"
        f"SITATER SOM SKAL VERIFISERES:\n{quotes_block}\n\n"
        "Returner én verdict per sitat, i samme rekkefølge."
        f"{forced_note}"
    )

    model = PRIMARY
    while True:
        try:
            # None keeps the agent's own (metered) primary instance
            run_model = None if model == PRIMARY else metered(model)
            result = await _agents[nynorsk].run(
                user_prompt,
                model=run_model,
                output_type=_output_type(model),
                model_settings=run_settings(model),
            )
            run_usage.add(model, "quote_verify", result)
            return result.output.verdicts
        except RECOVERABLE_ERRORS as exc:
            retry = retry_model(exc, model)
            if retry is None:
                raise
            log.warning("%s failed on %s; retrying with %s", type(exc).__name__, model, retry)
            model = retry

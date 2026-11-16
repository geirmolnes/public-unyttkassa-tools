"""Norwegian news article prompt construction (sitat + manus variants)."""

import unytt_editorial as ed

# The shared SPRAK block hardcodes Bokmål; swap the målform line when the
# source material is Nynorsk. A test guards against the constitution
# rewording the line silently.
_SPRAK_BOKMAL_LINE = "Skriv på norsk bokmål."
_SPRAK_NYNORSK = ed.SPRAK.replace(_SPRAK_BOKMAL_LINE, "Skriv på norsk nynorsk.")


def _shared(*, nynorsk: bool = False) -> str:
    """Universal newsroom rules from the shared constitution (also used by kaia);
    kladd adds its writer role, format and language specifics. Variant tails
    (sitatsak / manus) are appended by system_prompt()."""
    sprak = _SPRAK_NYNORSK if nynorsk else ed.SPRAK
    return f"""\
Du er en erfaren norsk journalist som skriver nyhetsartikler basert på kildemateriale.

## Regler

{ed.KILDETROSKAP}
- Hvis kildematerialet er utilstrekkelig, skriv det du kan og merk hva som mangler.

### Format — omvendt pyramide
- **Tittel**: Kort, presis overskrift (formatert som `#`).
- **Ingress**: Én eller to setninger i bold, maks 20 ord. Skal utfylle tittelen — ikke gjenta samme poeng. Ingen lenker i ingressen.
- **Brødtekst**: Utdypende avsnitt UTEN mellomtitler. Første setning i brødteksten skal IKKE gjenta poenget fra tittel eller ingress — gå rett på nytt innhold.

{ed.SITAT_FORMAT}

{ed.ANFORSELSTEGN}

{sprak}
- Bruk nåtid eller preteritum etter kontekst.
- Hold tonen nøytral og saklig, med korte setninger.

### Flerspråklige kilder
- Oversett all relevant informasjon til norsk.
- Ikke referer til originalspråket i artikkelen.

{ed.BALANSE}

{ed.MOTSTRIDENDE}"""


_SITATSAK_TAIL = """\

### Sitatsak — regler for artikler basert på eksterne nyhetsartikler
Når kildematerialet er en eller flere nyhetsartikler fra andre medier, skal du skrive en **sitatsak** (sitatartikkel) i NTB-stil:

- **Kort og strengt prioritert**: Sitatsaken skal typisk være 110–180 ord brødtekst (ikke medregnet tittel/ingress). Identifiser de 2–3 viktigste poengene (helst 2) og skriv kun om disse. Dropp utdypende detaljer, bakgrunnsstoff, sekundære kilder og perifere eksempler. Hvis originalartikkelen har ti poenger, skal sitatsaken ha to eller tre.
- **Balanse går foran korthet**: Dersom noen kritiseres, teller tilsvaret/forsvaret som ett av de 2–3 kjernepunktene — det er IKKE en «utdypende detalj», «bakgrunnsstoff» eller «sekundær kilde» som kan kuttes etter regelen over. Er plassen knapp, kort heller ned selve kritikken så den balanseres, framfor å droppe tilsvaret.
- **Få direkte sitater**: Velg de sterkeste og mest nyhetsverdige sitatene. Resten oppsummeres indirekte. Bruk gjerne flere sitater hvis de tilfører ulike perspektiver eller er nødvendige for balanse.
- **Tidlig kildehenvisning**: Kilden (mediet) skal navngis og lenkes i første avsnitt av brødteksten (IKKE i ingressen). Bruk inline-lenke i markdown — vis medienavnet som lenketekst, ikke URLen. Eksempel: `skriver [Khrono](https://www.khrono.no/...)`. ALLE kildemedier som brukes MÅ navngis og lenkes — dette er en absolutt regel, ikke valgfri. Hvis kildens URL er fra khrono.no, skal navnet «Khrono» nevnes med markdown-lenke i første avsnitt. Hvis det er fra folkeskolen.dk, skal «Folkeskolen» lenkes.
- **Tydelig kildereferanse vs. møtested**: Når mediet nevnes som kilde, skriv det entydig som referanse (f.eks. `Det skriver [Folkeskolen](URL)` på slutten av første avsnitt). UNNGÅ formuleringer som plasserer mediet midt i en setning på en måte som kan forveksles med møtested eller intervjumotpart, f.eks. «hun i Folkeskolen fortalte om...» — dette kan leses som om hun var i Folkeskolen. Skriv heller om: «Hun fortalte om sine erfaringer i et intervju med [annet medium]. Det skriver [Folkeskolen](URL).»
- **NTB-stil**: Nøkternt, nøytralt, telegramstil. Korte setninger. Ingen egne vurderinger.
- **Sitatmerking**: Direkte sitater MÅ tydelig attribueres med hvem som sier det og til hvilket medium. Bruk tankestrek foran direkte sitater:
  `– Dette er et sitat, sier Navn Navnesen til Khrono.`
  Det må være krystallklart at sitatene ikke er våre egne.
- Sitater skal ALLTID gjengis HELT ORDRETT. Et sitat kan forkortes ved å stryke hele setninger fra starten eller slutten — aldri inne i midten, og aldri omformulert.
- ALDRI gjør om indirekte sitater til direkte sitater. Hvis kilden skriver «Trump sa at det ikke vil bli noen anriking», skal du IKKE lage et direkte sitat av dette (– Det vil ikke bli noen anriking …). Gjengi det indirekte, slik kilden gjør.
- Velg sitater som er talende for saken og tilfører ulike stemmer eller perspektiver.
- Skriv en selvstendig artikkel med omvendt pyramide — ikke gjenta originalen avsnitt for avsnitt.
- **Alle medier som brukes som kilde MÅ siteres med lenke** i artikkelen. Hvis kildematerialet inneholder flere medier, skal hver av dem navngis og lenkes der informasjonen deres brukes.
"""


_MANUS_TAIL = """\

### Lengde og økonomi
Du skriver en selvstendig journalistisk artikkel basert på råmateriale som kan være intervjuutskrifter, rapporter, allerede skrevet tekst, lenker eller en blanding.

- **Fleksibel lengde, 200–800 ord brødtekst**: Tilpass lengden til stoffet. De fleste saker skal være korte.
- **Lengden må fortjenes**: En lang artikkel MÅ rettferdiggjøre lengden med reelt innhold. Ingen fyll, ingen oppblåsing. Når du er i tvil, skriv kortere.
- **Aldri gjenta et poeng**: Hvert avsnitt skal tilføre noe nytt. Ikke si det samme på en ny måte.
- **Effektivt og flytende språk**: Teksten skal flyte godt og lese lett, men være stram. Kutt setninger som ikke tilfører informasjon.
- **Prioriter**: Identifiser de viktigste poengene og bygg artikkelen rundt dem. Dropp perifere detaljer selv om de står i kildematerialet.
- Følg konvensjonene for nyhets-/featurejournalistikk. Ikke skriv en langlesningsartikkel.
- Når kilder navngis (f.eks. et medium en lenke peker til), navngi og lenk dem der informasjonen deres brukes.
"""


def system_prompt(*, manus: bool = False, nynorsk: bool = False) -> str:
    """Assemble the system prompt for the given product and målform."""
    tail = _MANUS_TAIL if manus else _SITATSAK_TAIL
    return _shared(nynorsk=nynorsk) + tail


SITAT_SYSTEM_PROMPT = system_prompt(manus=False)
MANUS_SYSTEM_PROMPT = system_prompt(manus=True)

# Backward-compatible alias (old imports reference SYSTEM_PROMPT).
SYSTEM_PROMPT = SITAT_SYSTEM_PROMPT


def build_user_prompt(
    formatted_sources: str,
    source_count: int,
    *,
    all_urls: bool = False,
    manus: bool = False,
    instructions: str | None = None,
    rules: list[str] | None = None,
) -> str:
    """Build user prompt with formatted source material.

    In manus mode the article type is fixed (journalistic), so all_urls is ignored.
    Admin-set standing rules, then optional per-draft journalist instructions
    (vinkel, fokus, lengde), are appended after the task line; both steer within
    the rules, never override them.
    """
    if manus:
        prefix = (
            f"Skriv en journalistisk artikkel basert på følgende {source_count} "
            f"kilde(r). Tilpass lengden til stoffet (200–800 ord)."
        )
    elif all_urls:
        prefix = (
            f"Skriv en sitatsak basert på følgende {source_count} "
            f"nyhetsartikkel(er). Følg sitatsak-reglene."
        )
    else:
        prefix = f"Skriv en nyhetsartikkel basert på følgende {source_count} kilde(r)."
    rule_lines = [r.strip() for r in rules or [] if r.strip()]
    if rule_lines:
        prefix += (
            "\n\nFaste regler fra redaksjonen (følg dem så langt de ikke bryter "
            "med reglene over; ved konflikt går journalistens instruks foran):\n"
            + "\n".join(f"- {r}" for r in rule_lines)
        )
    if instructions and instructions.strip():
        prefix += (
            "\n\nInstruks fra journalisten (følg den så langt den ikke bryter "
            f"med reglene over): {instructions.strip()}"
        )
    return f"{prefix}\n\n{formatted_sources}"

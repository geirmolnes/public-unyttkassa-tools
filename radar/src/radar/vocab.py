"""Lexical vocabulary for the education prefilter (nb/nn, sv, da).

Terms are split by *matching strategy*, not by language. Nordic compounding
means a root like «skole» keeps its meaning inside compounds — skolestart,
folkehøgskole, skolepolitikk — so it is matched anywhere in a word. «elev» is
not like that: it hides inside «relevant» and «elevator», so it is matched from
a word boundary only. Getting this backwards is the single biggest source of
error in this layer, in both directions.
"""

from __future__ import annotations

# Matched anywhere inside a word. Only for roots that survive compounding and
# do not occur inside unrelated words.
COMPOUND_ROOTS: list[str] = [
    # school itself (no / nn / sv / da)
    "skole", "skule", "skola", "skolan", "skolor", "skoling",
    # early childhood
    "barnehage", "barnehagelær", "førskole", "förskol", "forskol",
    "børnehave", "barnomsorg", "dagtilbud", "daginstitution", "dagmamma",
    "oppvekst",
    # teaching and curriculum
    "undervis", "læreplan", "läroplan", "læseplan", "fagfornyelse",
    "pensum", "kompetansemål", "vurderingsforskrift",
    # stages. «gymnasium» is spelled out because «gymnasie» does not reach it and
    # the shorter «gymnas» would swallow «gymnastikk».
    "videregående", "videregåande", "gymnasie", "gymnasium", "ungdomstrinn",
    "mellomtrinn", "barnetrinn", "ungdomsuddannelse",
    # vocational
    "lærling", "læreplass", "fagbrev", "yrkesfag", "yrkeslärar", "lærlinge",
    # the profession and its training
    "lærerutdanning", "lærarutdanning", "lärarutbildning", "læreruddannelse",
    "lærerstudent", "lärarstudent", "lærerprofesjon", "lærernorm", "lærertetthet",
    "lärarbrist", "lærermangel", "pedagog", "pædagog",
    # institutions, law, politics
    "utdanningsdirektorat", "kunnskapsdepartement", "kunnskapsminister",
    "utdanningsminister", "utdanningspolitikk", "utdanningsforbund",
    "skolverket", "skolinspektion", "lærerforening", "lärarförbund",
    "opplæringslov", "barnehagelov", "folkeskolelov", "skollag",
    "friskole", "friskola", "privatskole", "folkehøgskole", "folkhögskol",
    # school life
    "leksehjelp", "skolemat", "mobbeombud", "fritidshem", "fritidspedagog",
    "spesialundervisning", "specialundervisning", "specialpedagog",
    "elevråd", "elevorganisasjon", "elevundersøkelsen", "foreldreutvalg",
    "barnehagestyrer", "standpunktkarakter",
]

# Matched from a word boundary, any suffix allowed: `\belev\w*` catches elev,
# elever, elevene, elevrådet — but not relevant or elevator.
STEM_TERMS: list[str] = [
    "elev", "lærer", "lærar", "lärar",
    "rektor", "skoleleder", "skolesjef",
    "læring", "lärande", "klasserom", "klassrum", "klasseværelse",
    "mobbing", "mobbning", "mobning",
    "eksamen", "eksamin", "examen", "avgangsprøv", "avgangskarakter",
    "standpunktkarakter", "skulk", "skolefravær",
]

# Words that begin with a stem but mean something else entirely. `\belev\w*`
# matches «elevator» — the boundary is at the start, and \w* happily eats the
# rest. «styrer» (governs) and «standpunkt» (stance) were stems until they
# turned out to be ordinary Norwegian; they live on as compounds instead.
STEM_STOPWORDS: frozenset[str] = frozenset(
    {
        "elevator", "elevatoren", "elevatorer", "elevatorene",
        "elevere", "elevert", "elevering",
        "lærerik", "lærerikt", "lærerike",
    }
)

# Case-sensitive, whole-token. Lowercasing these would match noise («sfo» inside
# a URL slug, «aks» inside a Swedish word).
ACRONYMS: list[str] = [
    "SFO", "AKS", "PPT", "Udir", "PISA", "TIMSS", "PIRLS", "LK20", "KD",
    "SkoleFAM", "NOKUT", "SFI", "PP-tjenesten",
]

# Multi-word names that carry no single distinctive root.
PHRASES: list[str] = [
    "nasjonale prøver",
    "nationella prov",
    "sveriges lärare",
    "danmarks lærerforening",
    "norsk lektorlag",
    "skolens ledelse",
    "de nasjonale prøvene",
    "tidlig innsats",
    "tilpasset opplæring",
    "trygt og godt skolemiljø",
]

# Publications whose whole output is education. They skip the lexical gate —
# but not the LLM, which still has to score them.
EDUCATION_NATIVE_SOURCES: frozenset[str] = frozenset(
    {"ViLärare", "Folkeskolen", "Barnehage"}
)

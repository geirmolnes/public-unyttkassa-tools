# Tilpass Radar til en annen redaksjon

Radar er skrevet for Utdanningsnytt, og utdanningsfeltet står hardkodet: i
ordlista, i promptene, i kategorinavnene og i tekstene på sidene. Ingenting av
det ligger i en konfigurasjonsfil. Denne guiden går gjennom hva som må endres, i
den rekkefølgen det lønner seg å gjøre det, og viser to gjennomførte eksempler:
en avis som dekker Forsvaret, og en som dekker helse.

Alle stier er relative til `src/radar/`.

## Før du begynner

Få Radar til å kjøre uendret først (se README), med deres egne kilder i
`feeds.txt`. Da vet du at innhenting, database og webserver virker før du rører
innholdet.

Gjør tilpasningen før dere har data dere vil beholde. Kategoriene lagres som
tekst på hver dom, så det enkleste mens du prøver deg fram, er å slette
`data/utdanningsradar.db` og kjøre `radar-tick` på nytt. Artikkelbasen
(`data/news_scraper.db`) kan stå.

Det er to lag som avgjør hva som havner i feeden, og de stilles inn hver for seg:

| Lag | Fil | Spørsmål | Feil den kan gjøre |
| --- | --- | --- | --- |
| Ordfilter | `vocab.py`, `prefilter.py` | Nevner artikkelen feltet i det hele tatt? | Kaster noe relevant. Da ser modellen det aldri. |
| Språkmodell | `classify.py` | Handler den *om* feltet, og hvor viktig er den? | Slipper gjennom støy, eller scorer feil. |

Ordfilteret skal derfor være romslig. Modellen sier nei.

## 1. Kildene

`feeds.txt` avgjør hva Radar leser. Tre steder i koden kjenner kildenavn, og
navnet må stå nøyaktig som i `feeds.txt`:

- `vocab.py`: `EDUCATION_NATIVE_SOURCES`. Kilder der alt er relevant stoff. De
  går forbi ordfilteret, men scores fortsatt av modellen. Bytt ut med deres
  egne fagmedier, eller la mengden stå tom.
- `web/queries.py`: `SOURCE_LABELS`. Visningsnavn der navnet i basen er et annet
  enn det leserne kjenner (`"Forskning": "Forskning.no"`). Kan tømmes.
- `dagbladet.py`: `SOURCE = "Dagbladet"`. Retter opp to særheter ved Dagbladets
  sider. Gjør ingenting for andre kilder, og kan stå.

## 2. Nedslagsfeltet: `classify.py`

Dette er den viktigste filen. Alt modellen vet om redaksjonen, står i
`SYSTEM_PROMPT`.

**`SYSTEM_PROMPT`.** Skriv om disse delene, og behold oppbyggingen:

- Første avsnitt: hvem redaksjonen er, hva feltet omfatter, og hvem leserne er.
- `RELEVANT er saker om:` Lista over stoffområder. Vær konkret. Punktet om
  kriminal- og tilsynssaker viser hvor presist det lønner seg å være: det kom
  til fordi modellen kastet pågripelser der en lærer var siktet.
- `IKKE RELEVANT:` Det som ligner, men ikke er deres. Det er her presisjonen
  kommer fra.
- Avsnittet som starter med «Vær særlig streng med høyere utdanning». Dette er
  vår vanskeligste grense: et nabofelt som deler ord og kilder med vårt, men som
  vi ikke dekker. Finn deres tilsvarende grense og skriv den like tydelig. Har
  dere ingen, stryk avsnittet.
- `kind sier hva slags tekst det er:` Definisjonene av nyhet, debatt, forskning
  og fagstoff. De er ganske allmenne. Bytt eksemplene («bacheloroppgaver og
  prisnominasjoner», «didaktikk»).
- `score er 0-100`: Skalaen måler nyhetsverdi for redaksjonen, ikke hvor godt
  saken treffer temaet. Hos oss er en viktig bruk å finne saker det kan lages
  sitatsak av, og det står i teksten. Skriv hva *dere* bruker radaren til.

**`STAGES`.** Kategoriene feeden kan filtreres på. Hos oss er det trinn i
utdanningsløpet. Velg fire til seks inndelinger som passer feltet deres. Tre
regler:

- Nøklene er små bokstaver uten mellomrom og uten æ, ø og å (`sykehus`,
  `psykisk_helse`). De brukes i adresser, i databasen og som CSS-klasser.
- Behold `ikke_relevant` sist i lista. `_one_of` behandler den som «ingen
  kategori».
- Behold en samlekategori. Heter den noe annet enn `tverrgaende`, må du også
  endre reserveverdien i `classify()` (`"tverrgaende" if relevant else None`).
  Enklest er å la nøkkelen stå og bare gi den et nytt visningsnavn i steg 3.

`RESPONSE_SCHEMA` bygges av `STAGES` og `KINDS` og trenger ingen endring.

**`KINDS`.** Nyhet, debatt, forskning, fagstoff. La stå med mindre dere trenger
en annen inndeling. Endrer du den, følger `KIND_LABELS_NO` og `KIND_VALUES` i
`web/searchq.py` med, og reserveverdien `"nyhet"` i `classify()` og i
`web/templates/_macros.html` (`it.kind != 'nyhet'`).

**`PROMPT_VERSION`.** Lagres på hver dom. Hver gang du endrer prompten: øk
versjonen og kjør

```bash
uv run radar-reclassify --stale-prompt
```

Da scores bare det som ble vurdert med en eldre prompt, på nytt.

## 3. Kategoriene i grensesnittet

`STAGES` må speiles tre steder, ellers vises rå nøkler eller fargeløse prikker.

**`web/searchq.py`**

- `STAGE_LABELS`: nøkkel → visningsnavn. Styrer menyen til venstre og
  filterpanelet. Rekkefølgen her er rekkefølgen på skjermen. `ikke_relevant`
  skal ikke være med.
- `STAGE_VALUES`: det brukeren kan skrive etter `trinn:` → nøkkel. Ta med
  skrivemåter med æ, ø og å og vanlige forkortelser (`"vgs": "videregaende"`).
- `KEY_ALIASES`: selve nøkkelordene i søket. `trinn:` gir liten mening for en
  helseavis. Det enkleste er å legge til et alias (`"omrade": "trinn"`,
  `"område": "trinn"`). Da kan desken skrive `område:sykehus`, men søkefeltet
  viser fortsatt `trinn:sykehus` når noen klikker en kategori. Vil dere bli
  kvitt ordet helt, må det byttes i `_apply`, `from_controls` og `from_legacy`
  her, i `_predicates` og `facet_counts` i `web/queries.py`, i parameterne til
  `feed` i `web/app.py`, og i `base.html` og `feed.html` (`with_token('trinn', …)`,
  `name="trinn"`, `facets.trinn`).

**`web/static/style.css`**

Hver kategori har en farge. De står to steder i fila: først som
`.stage-barnehage` til `.stage-tverrgaende`, og lenger ned en gang til under
«dark theme», der variablene `--stage-barnehage` til `--stage-tverrgaende` gir
lysere farger i mørkt tema. Slett de gamle begge steder og lag én klasse per
nøkkel. Én linje holder hvis samme farge kan brukes i begge temaer:

```css
.stage-sykehus { --stage: #D94F1E; }
```

**Ordet «Trinn»** står som synlig tekst i `web/templates/feed.html`
(`<legend>Trinn</legend>`), `web/templates/base.html` (`aria-label="Trinn"`) og
`web/templates/story.html` (`<span class="k">Trinn</span>`). Bytt til deres ord.

## 4. Ordfilteret: `vocab.py`

Ordlista er delt etter *hvordan* et ord matches, ikke etter språk. Å legge et ord
i feil liste er den vanligste feilen, i begge retninger.

| Liste | Matcher | Bruk til | Skriv |
| --- | --- | --- | --- |
| `COMPOUND_ROOTS` | hvor som helst inni et ord | røtter som beholder betydningen i sammensetninger og ikke finnes inni andre ord | små bokstaver |
| `STEM_TERMS` | fra ordgrense, med hvilken som helst endelse | ord som gjemmer seg inni urelaterte ord | små bokstaver |
| `STEM_STOPWORDS` | hele ordet | ord som begynner på en stamme, men betyr noe annet | små bokstaver |
| `ACRONYMS` | hele ordet, store og små bokstaver teller | forkortelser og egennavn | nøyaktig som de skrives |
| `PHRASES` | ordrett, hvor som helst | flerordsnavn uten én særpreget rot | små bokstaver |

Norsk, svensk og dansk setter sammen ord. «skole» betyr det samme i skolestart,
folkehøgskole og skolepolitikk, så den er en sammensetningsrot og fanger alle
tre. «elev» er ikke sånn: den ligger inni «relevant» og «elevator». Derfor er
den en stamme som bare matcher fra ordgrense. Da treffer den fortsatt
«elevator», for ordgrensen er i starten, og derfor finnes `STEM_STOPWORDS`.

Test hvert ord du legger inn mot spørsmålet: *finnes dette inni et ord som ikke
har noe med feltet å gjøre?*

- Nei → `COMPOUND_ROOTS`.
- Ja, men bare som start på noen få kjente ord → `STEM_TERMS`, og de kjente
  ordene i `STEM_STOPWORDS`.
- Ja, mange → dropp roten. Bruk lengre, entydige sammensetninger i stedet.

Eksemplene under har flere slike feller: «kreft» ligger inni «krefter»,
«forsvar» er også en forsvarsadvokat og en midtstopper, og «hær» åpner
«hærverk».

`ACRONYMS` er eneste liste som skiller store og små bokstaver. Bruk den også for
egennavn som bare betyr noe med stor forbokstav: `"Forsvaret"` er etaten,
«forsvaret» er laget som slapp inn tre mål.

Vi leser nordiske kilder, så listene har bokmål, nynorsk, svensk og dansk. Les
dere bare norske kilder, trenger dere ikke de andre.

**Tersklene** står i `prefilter.py`:

- `_LEDE_CHARS = 500`: ett sterkt ord i tittel, ingress eller de første 500
  tegnene slipper artikkelen gjennom.
- `_MIN_BODY_ROOTS = 3` og `_MIN_BODY_HITS = 6`: lenger ned kreves tre ulike ord
  eller seks treff totalt. Med to slapp kriminal- og utenriksstoff gjennom hos
  oss. Start med disse og endre først når `/frafiltrert` viser at det trengs.

Begrunnelsene `"education-native source"` og `"no education vocabulary"` i
`prefilter()` vises på `/frafiltrert`. Det samme gjør reserveteksten
`'ingen utdanningsord'` i `web/templates/rejected.html`. Kosmetikk, men bytt dem.

Ordfilteret koster ingenting å prøve:

```bash
uv run radar-tick --reingest --no-llm   # les alt på nytt gjennom det nye filteret
```

`--reingest` nullstiller også modellens dommer for artiklene den leser på nytt,
så neste vanlige `radar-tick` klassifiserer dem igjen.

## 5. Forløp og dekning

**`cluster.py`** knytter artikler om samme hendelse sammen. `SYSTEM_PROMPT`
nevner utdanningsfeltet i første setning, har et avsnitt om at norsk, svensk og
dansk skolepolitikk er ulike saker («Land og skolesystem …»), og to lister med
eksempler (`SAMME SAK` og `IKKE SAMME SAK`) hentet fra vår dekning. Reglene er
allmenne og kan stå. Bytt første setning og eksemplene med par fra deres eget
felt, helst ekte titler der dere vet svaret. Øk `PROMPT_VERSION` og bygg
forløpene på nytt:

```bash
uv run radar-recluster
```

**`coverage.py`** svarer på «har vi skrevet om dette?». Den kjører bare når
arkivet finnes (README, «Har vi den?»). `SYSTEM_PROMPT` og
`NOVELTY_SYSTEM_PROMPT` nevner Utdanningsnytt ved navn og har eksempler
(`DEKKET` og `IKKE DEKKET`) fra vår dekning. Bytt navn og eksempler, øk
`PROMPT_VERSION` og kjør

```bash
uv run radar-coverage --stale-prompt
```

**`archive.py`**: `NOT_REPORTING` er seksjonene i deres eget arkiv som ikke
teller som redaksjonens dekning. Hos oss debatt, fagartikler og annonser. Et
leserinnlegg som lenker til fire saker, skal ikke merke alle fire som dekket.
Verdiene sammenlignes med kolonnen `article_section`.

**Uten eget arkiv.** Steget hopper over seg selv, men to ting må ryddes i
`web/templates/feed.html`:

- Blokken `{% if stats.archive_seen == 0 %}` viser en gul stripe om at «Har vi
  den?» er ubesvart. Uten arkiv står den der alltid, og skjuler varselet om at
  radaren har stoppet. Fjern den grenen og gjør `{% elif stale_hours %}` om til
  `{% if stale_hours %}`.
- Feltet `<legend>Har vi den?</legend>` i filterpanelet filtrerer på noe som
  aldri blir satt. Fjern det.

## 6. Navn, lenker og tekster

**Navnet «Utdanningsradar»** står i

- `web/templates/base.html`: `<title>`, `mobile-brand` og `brand-mark`
- `web/templates/feed.html`: `<title>` og `<h1 class="sr-only">`
- `web/templates/story.html`, `rejected.html` og `about.html`: `<title>`
- `web/app.py`: `FastAPI(title="Utdanningsradar")` i `create_app`

**`web/templates/about.html`** er siden «Om radaren». Den forklarer rangeringen
for journalistene og nevner utdanningsstoff, fagstoff og didaktikk. Skriv den om
når promptene er på plass, så den beskriver det modellen faktisk gjør.

**Lenken øverst i menyen** (`rail-home` i `web/templates/base.html`) går til
`UNYTTKASSA_URL` med teksten «Unyttkassa», vår interne verktøyside. Pek den til
deres egen startside og bytt teksten, eller fjern lenken.

**Knappen «Lag sitatsak»** (`web/templates/story.html`) sender saken til
`SITATKLADD_URL` som `<SITATKLADD_URL>/?url=<artikkelens adresse>`, bygget av
`sitatkladd_link` i `web/app.py`. Hos oss er det et eget verktøy som skriver
utkast til sitatsak. Har dere noe som tar imot en adresse på den måten, pek
variabelen dit. Hvis ikke, fjern `<a class="btn ghost" …>Lag sitatsak</a>`.

**Eksempeltekster** i `web/templates/feed.html`: `placeholder="f.eks. PISA"` og
«søket forstår sammensetninger, så «skole» finner også «skolestart»».

**Innlogging.** `LOGIN_URL` i `web/auth.py`. Se README.

## 7. Drift

- `cli.py`, `doctor`: sjekker at søkeindeksen virker ved å søke på `"skole"*`,
  og melder feil når ingen artikler treffer. Bytt til et ord som alltid finnes
  i deres stoff.
- `config.py`, `DB_PATH`: databasefila heter `utdanningsradar.db`, og `backup` i
  `cli.py` lager `utdanningsradar-<dato>.db`. Vil dere ha et annet navn, endre
  alle tre stedene før første kjøring. Det er ingen grunn til å gjøre det etterpå.
- `config.py`, `OSLO`: tidssonen feeden grupperer dager i.
- `config.py`, `GEMINI_MODEL`: standardmodellen. Alle fire stegene bruker den.

## 8. Det som kan stå

| Fil | Hvorfor |
| --- | --- |
| `text.py` | Renser artikkelsider for norsk sidepynt (`_RAIL_HEADINGS`, `_SUMMARY_WIDGET_HEADINGS`, `_CREDIT_PREFIX`). Ikke knyttet til feltet. Utvid listene hvis kildene deres har annet pynt. |
| `urls.py` | Normaliserer adresser (`TRACKING_PARAMS`, `SAFELINKS_HOST`). Allment. |
| `dagbladet.py` | Særheter ved én kilde. Slår bare inn for kildenavnet «Dagbladet». |
| `storage.py` | Skjema og søkeindeks. Indeksen er stilt inn for nordiske språk. Endrer du tokenizeren, øk `FTS_VERSION`. |
| `source.py`, `feeds.py`, `pipeline.py`, `gemini.py` | Rørgangen. Vet ingenting om feltet. |
| `web/searchq.py` utenom listene i steg 3, `web/mute.py`, `web/static/cmd.js` | Søkegrammatikk og grensesnitt. |

## 9. Testene

`uv run pytest` bruker utdanningseksempler og vil feile når du endrer ordliste og
kategorier. Det er meningen. Disse må skrives om med deres ord:

- `tests/test_prefilter.py`: ordfilteret. Skriv denne først. Den er den beste
  sikringen mot en rot som slipper gjennom alt.
- `tests/test_searchq.py`, `tests/test_routes.py`: `trinn:`-verdiene og
  visningsnavnene.
- `tests/test_ingest.py`, `tests/test_stories.py`, `tests/test_cluster.py`,
  `tests/test_display.py`: har kategorinøklene i testdata, og kan feile.

Testene for `text.py`, `urls.py`, `feeds.py`, `dagbladet.py` og innloggingen er
uavhengige av feltet.

## Eksempel: Forsvar

En avis som dekker Forsvaret og forsvars- og sikkerhetspolitikk.

**Første avsnitt i `SYSTEM_PROMPT`:**

```
Du vurderer nyhetsartikler for en redaksjon som dekker Forsvaret og norsk
forsvars- og sikkerhetspolitikk.

Nedslagsfeltet er forsvarsgrenene, Heimevernet og resten av forsvarssektoren —
pluss forsvars- og sikkerhetspolitikken som styrer dem. Leserne er ansatte og
vernepliktige i Forsvaret, veteraner, forsvarsindustrien og politikere på feltet.

RELEVANT er saker om:
- forsvarsbudsjett, langtidsplan, lovverk og organisering av sektoren
- materiell, anskaffelser og forsvarsindustri
- øvelser, operasjoner og beredskap, også alliert aktivitet i Norge og nordområdene
- verneplikt, førstegangstjeneste, rekruttering, utdanning og personellvilkår
- veteraner og oppfølgingen av dem
- Nato, forsvarssamarbeid og avtaler der Norge er part
- krig og konflikt ute når det endrer trusselbildet for Norge eller forpliktelsene
  våre: Ukraina-krigen, russisk aktivitet i nord, alliertes disposisjoner
- tilsyn, granskinger, varsling og rettssaker der Forsvaret eller ansatte i
  kraft av stillingen er part

IKKE RELEVANT:
- utenriksnyheter om krig og konflikt uten betydning for norsk eller alliert forsvar
- politi, tollvesen og sivil redningstjeneste når Forsvaret ikke er involvert
- krigshistorie, jubileer og minnemarkeringer uten nyhetskrok
- sport, kultur og kjendisstoff der en person tilfeldigvis er eller har vært soldat
- saker der «forsvar» betyr noe annet: forsvarere i retten og på fotballbanen

Vær særlig streng med sivil beredskap. Strømforsyning, matlagre, helseberedskap
og kommunenes beredskapsplaner er utenfor feltet, med mindre saken handler om
totalforsvaret og Forsvarets rolle i det.
```

**`STAGES` og `STAGE_LABELS`:**

```python
STAGES = [
    "haeren", "sjoforsvaret", "luftforsvaret", "heimevernet",
    "sikkerhetspolitikk", "tverrgaende", "ikke_relevant",
]

STAGE_LABELS = {
    "haeren": "Hæren",
    "sjoforsvaret": "Sjøforsvaret",
    "luftforsvaret": "Luftforsvaret",
    "heimevernet": "Heimevernet",
    "sikkerhetspolitikk": "Politikk",
    "tverrgaende": "Felles",
}
```

I `STAGE_VALUES` tar du med `"hæren"`, `"hær"`, `"sjøforsvaret"`, `"marinen"`,
`"hv"` og `"politikk"` som skrivemåter, og i `KEY_ALIASES` for eksempel
`"gren": "trinn"`.

**`vocab.py`:**

```python
COMPOUND_ROOTS = [
    # «forsvar» alene er også advokater og midtstoppere. Bare entydige sammensetninger.
    "forsvarssjef", "forsvarsminister", "forsvarsdepartement", "forsvarsbudsjett",
    "forsvarsmateriell", "forsvarsbygg", "forsvarsindustri", "forsvarsevne",
    "forsvarspolitikk", "forsvarssektor", "forsvarsstab", "forsvarsavtale",
    "forsvarskommisjon", "forsvarsløft",
    "luftforsvar", "sjøforsvar", "cyberforsvar", "totalforsvar", "heimevern",
    "militær", "militär", "verneplikt", "førstegangstjeneste", "soldat",
    # «rekrutt» ligger inni «rekruttering». Skolen er entydig.
    "offiser", "rekruttskole", "befalsskole", "krigsskole",
    "kampfly", "fregatt", "ubåt", "korvett", "stridsvogn", "artilleri",
    "luftvern", "krigsskip", "marinejeger", "spesialstyrke", "kystjeger",
    "etterretning", "sikkerhetspolitikk",
    # Fanger «nato-møtet» og «nato-land». Selve ordet står i ACRONYMS.
    "nato-",
]

STEM_TERMS = [
    "hær", "befal", "marinen", "veteran", "garde", "kystvakt", "brigade", "bataljon",
]

STEM_STOPWORDS = frozenset({
    "hærverk", "hærverket", "hærtatt",
    # «befal» ligger også inni «anbefaler», som ordgrensen allerede holder ute.
    "befaling", "befalinger", "befalte", "befaler", "befale",
    "veteranbil", "veteranbiler", "veteranbåt", "veteranbåter",
    "garderobe", "garderoben", "garderober", "garderobene", "gardermoen",
})

ACRONYMS = [
    # Med stor F er det etaten.
    "Forsvaret",
    # Norske aviser skriver begge deler.
    "NATO", "Nato",
    "FFI", "FMA", "FOH", "NSM", "KNM", "HV",
    "E-tjenesten", "F-35", "P-8", "NASAMS", "CV90",
]

PHRASES = [
    "norske styrker", "væpnede styrker", "brigade nord", "h.m. kongens garde",
    "forsvarets operative hovedkvarter", "forsvarets forskningsinstitutt",
    "cold response", "joint viking", "nordic response",
]
```

**Kilder.** Disse feedene svarte da guiden ble skrevet:

```
Forsvarets forum | https://www.forsvaretsforum.no/?lab_viewport=rss
Aldrimer | https://www.aldrimer.no/feed/
Regjeringen | https://www.regjeringen.no/no/rss/Rss/2581966/?documentType=aktuelt/nyheter
NRK | https://www.nrk.no/toppsaker.rss
```

Fagmedier dere ikke selv er, er kandidater til listen over kilder som går forbi
ordfilteret (`EDUCATION_NATIVE_SOURCES`), for eksempel `{"Aldrimer"}`. Vi fant
ikke feed-adressen til Forsvarets egne nyheter, High North News eller
Stortinget. Let etter dem selv.

## Eksempel: Helse

En avis for dem som jobber i helse- og omsorgstjenesten.

**Første avsnitt i `SYSTEM_PROMPT`:**

```
Du vurderer nyhetsartikler for en redaksjon som dekker helse- og
omsorgstjenesten i Norge.

Nedslagsfeltet er sykehusene, fastlegene og resten av kommunehelsetjenesten,
pleie og omsorg, psykisk helsevern og rusbehandling — pluss helsepolitikken og
forvaltningen som styrer dem. Leserne er leger, sykepleiere og annet
helsepersonell, ledere i tjenesten og helsepolitikere.

RELEVANT er saker om:
- helsepolitikk, lovverk, finansiering og organisering av tjenesten
- sykehusøkonomi, sykehusstruktur, ventetider og kapasitet
- fastlegeordningen, legevakt, sykehjem og hjemmetjenester
- bemanning, rekruttering, lønn, arbeidstid og arbeidsmiljø for helsepersonell
- pasientsikkerhet, tilsyn, pasientskader og varsling
- legemidler, nye behandlingsmetoder og beslutninger om hva som innføres
- smittevern, vaksinasjon og folkehelsetiltak
- forskning med betydning for behandling eller for hvordan tjenesten drives
- tilsyns-, straffe- og autorisasjonssaker der helsepersonell er part i kraft
  av yrket

IKKE RELEVANT:
- livsstil, kosthold, trening og velvære rettet mot allmennheten
- enkeltpersoners sykdomshistorier uten noe å si om tjenesten eller behandlingen
- ulykker og kriminalsaker der helsevesenet bare nevnes: «fraktet til sykehus»
- kjendisers helse
- dyrehelse og veterinærstoff

Vær særlig streng med forskningsformidling. En studie om at et matvarevalg
henger sammen med en sykdom, er helsejournalistikk for allmennheten og utenfor
feltet. Forskning er relevant når den kan endre behandling, retningslinjer eller
drift.
```

**`STAGES` og `STAGE_LABELS`:**

```python
STAGES = [
    "sykehus", "kommunehelse", "omsorg", "psykisk_helse",
    "folkehelse", "tverrgaende", "ikke_relevant",
]

STAGE_LABELS = {
    "sykehus": "Sykehus",
    "kommunehelse": "Fastlege og kommune",
    "omsorg": "Pleie og omsorg",
    "psykisk_helse": "Psykisk helse og rus",
    "folkehelse": "Folkehelse",
    "tverrgaende": "På tvers",
}
```

I `STAGE_VALUES`: `"fastlege"`, `"kommune"`, `"sykehjem"`, `"psykiatri"`, `"rus"`
og `"psykisk"` som skrivemåter, og `"felt": "trinn"` i `KEY_ALIASES`.

**`vocab.py`:**

```python
COMPOUND_ROOTS = [
    "sykehus", "sjukehus", "sjukhus", "sygehus", "helseforetak", "helsetjeneste",
    "helsevesen", "helsepersonell", "helseminister", "helsedirektorat", "helsetilsyn",
    "helsepolitikk", "helsestasjon", "helseplattformen", "folkehelse", "tannhelse",
    "sykepleie", "sjukepleie", "sjuksköter", "sygeplej", "helsefagarbeider",
    "jordmor", "jordmødre", "fastlege", "legevakt", "legemangel", "legemiddel",
    "legeforening", "sykehjem", "hjemmesykepleie", "hjemmetjeneste", "eldreomsorg",
    # «rus» alene ligger inni «russisk» og «Russland».
    "omsorgsbolig", "psykiatri", "rusbehandling", "rusomsorg", "rusmiddel",
    "rusavhengig", "ambulanse", "akuttmottak", "intensivavdeling", "fødeavdeling",
    "fødetilbud", "pasient", "kirurg", "vaksin", "smittevern", "pandemi",
    # «kreft» alene står i STEM_TERMS: den ligger inni «krefter».
    "kreftbehandling", "kreftpasient", "fysioterap", "ergoterap", "vernepleie",
    "bioingeniør",
]

STEM_TERMS = [
    "lege", "kreft", "diagnose", "sykdom",
    # Bred. Slipper gjennom mye livsstilsstoff som modellen må si nei til.
    # Se på /frafiltrert og på kostnaden etter en uke, og vurder å ta den ut.
    "helse",
]

STEM_STOPWORDS = frozenset({
    "legende", "legenden", "legender", "legendene", "legendarisk", "legendariske",
    "legering", "legeme", "legemet", "legemer",
    "krefter", "kreftene", "kreftenes",
    "helsekost", "helsestudio", "helsestudioet",
})

ACRONYMS = [
    "HOD", "FHI", "Hdir", "DMP", "NPE", "Ukom", "Helfo", "DPS", "BUP", "AMK",
    # Ikke «NSF»: det er også Norges Skiforbund.
    "KAD", "LIS1", "OUS", "UNN", "Ahus", "MRSA",
]

PHRASES = [
    "psykisk helse", "psykisk helsevern", "norsk sykepleierforbund",
    "den norske legeforening", "helse sør-øst", "helse vest", "helse midt-norge",
    "helse nord", "st. olavs hospital", "fritt behandlingsvalg", "pakkeforløp",
]
```

**Kilder.** Disse feedene svarte da guiden ble skrevet:

```
Dagens Medisin | https://www.dagensmedisin.no/?lab_viewport=rss
Sykepleien | https://sykepleien.no/rss.xml
Tidsskriftet | https://tidsskriftet.no/rss.xml
FHI | https://www.fhi.no/rss/nyheter/
Kommunal Rapport | https://www.kommunal-rapport.no/?lab_viewport=rss
Forskning | https://www.forskning.no/?lab_viewport=rss
NRK | https://www.nrk.no/toppsaker.rss
```

Kandidater til kilder som går forbi ordfilteret: `{"Dagens Medisin",
"Sykepleien", "Tidsskriftet"}`, minus den dere selv er.

Begge ordlistene er et utgangspunkt, prøvd mot noen hundre artikler fra fem
kilder. De er ikke stilt inn over tid slik vår egen er.

## Slik stiller du inn

**Ordfilteret først, uten modell.** Kjør `uv run radar-tick --reingest --no-llm`
og åpne `/frafiltrert`. Siden viser alt som ble kastet, med grunnen og ordene som
traff. Les titlene:

- Ser du saker som burde vært med, mangler et ord. Legg det i riktig liste.
- Linja fra `radar-tick` sier hvor mange som slapp gjennom
  (`passed_prefilter=`). Slipper nesten alt gjennom, er en rot for bred. Finn
  den ved å se hvilke ord som går igjen på artiklene som slapp gjennom uten å
  høre hjemme der.

Rundt 70 prosent faller ut hos oss. Tallet avhenger av kildene: leser dere mest
fagmedier, skal mer slippe gjennom.

**Så modellen.** Kjør `uv run radar-tick` og les feeden. Modellens begrunnelse
står under hver sak, og `/frafiltrert` viser begrunnelsen for det den sa nei til.
Feil kommer i to slag:

- Feil om *hva som er relevant*: stram inn eller utvid listene `RELEVANT` og
  `IKKE RELEVANT` i `SYSTEM_PROMPT`. Ett konkret punkt virker bedre enn et
  generelt adjektiv.
- Feil *score*: juster beskrivelsen av intervallene. `/frafiltrert` har en
  tabell over hvert scoreintervall, per promptversjon.

Etter hver promptendring: øk `PROMPT_VERSION`, kjør
`uv run radar-reclassify --stale-prompt`, og sammenlign.

**Tommel opp og ned** på sakssidene lagres per bruker. Be desken bruke dem de
første ukene. Sammen med «Har vi den?» er det den eneste målingen av treffsikkerhet
som finnes.

**Kostnad.** Hver artikkel som slipper gjennom ordfilteret, koster ett modellkall.
Relevante artikler koster i tillegg ett kall for forløp, og noen få for dekning
når arkivet finnes. `CLASSIFY_LIMIT` setter taket per kjøring.

## Sjekkliste

- [ ] `feeds.txt` har deres kilder, og `radar-fetch` leser dem uten feil
- [ ] `vocab.py`: alle fem listene og `EDUCATION_NATIVE_SOURCES` er byttet
- [ ] `/frafiltrert` etter `radar-tick --reingest --no-llm`: ingenting relevant er kastet
- [ ] `classify.py`: `SYSTEM_PROMPT` og `STAGES` er skrevet om, `PROMPT_VERSION` økt
- [ ] `web/searchq.py`: `STAGE_LABELS`, `STAGE_VALUES` og et alias i `KEY_ALIASES`
- [ ] `web/static/style.css`: én `.stage-<nøkkel>` per kategori, i lyst og mørkt tema
- [ ] Ordet «Trinn» er byttet i `feed.html`, `base.html` og `story.html`
- [ ] `cluster.py`: første setning og eksemplene i `SYSTEM_PROMPT`, `PROMPT_VERSION` økt
- [ ] `coverage.py` og `archive.py` er tilpasset, eller stripa og filteret er fjernet fra `feed.html`
- [ ] «Utdanningsradar» er byttet i malene og i `web/app.py`
- [ ] `about.html` beskriver deres radar
- [ ] Lenken «Unyttkassa» og knappen «Lag sitatsak» peker riktig eller er fjernet
- [ ] `LOGIN_URL` i `web/auth.py` peker på deres innlogging
- [ ] `doctor` i `cli.py` søker på et ord fra deres felt
- [ ] `tests/test_prefilter.py` er skrevet om, og `uv run pytest` er grønn
- [ ] Databasen er slettet og bygget på nytt med endelig ordliste og prompt

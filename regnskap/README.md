# Regnskap

Regnskap henter årsregnskap fra Brønnøysundregistrene for journalister: søk opp et selskap, se
nøkkeltallene over flere år, last ned årsregnskapene som PDF og les tallene og notene i dem.
Laget av [Utdanningsnytt](https://www.utdanningsnytt.no), der redaksjonen bruker det daglig.
Koden ligger ute slik at andre redaksjoner kan ta den i bruk og tilpasse den.

Tre deler, og bare den første virker uten noe mer enn koden:

| Del | Gjør | Trenger |
|---|---|---|
| Brreg | Søk på navn eller org.nr. Roller og nøkkeltall for alle år Brreg har som data, for selskap og konsern. Årsregnskap som PDF: ett år, flere år i én ZIP, eller en liste på opptil 50 org.nr. Kunngjøringer per tema, med søk. Favoritter og samlinger per bruker. | ingenting |
| Aksjonærregisteret | Eiere og eierandeler per år, endringer fra år til år, eierkart over konsernet oppover og nedover, personsøk. | `shareholders.db`, som du bygger selv |
| Språkmodeller | «Les inn»: hver side i PDF-en leses til tall, noter og tekst, kontrolleres og vises ved siden av den skannede siden, med søk. Datterselskaper fra noten i årsrapporten. Spør AI om selskapet. | `ANTHROPIC_API_KEY` og `GEMINI_API_KEY` |

## Kom i gang

Krever [uv](https://docs.astral.sh/uv/).

```bash
uv sync
./scripts/dev.sh
```

Åpne `http://127.0.0.1:8000`. `scripts/dev.sh` setter `DEV_INSECURE_AUTH=1`: alle slipper inn
som `dev@localhost`, som også er administrator. Det er bare for lokal bruk. Alt appen lagrer,
havner i `./data`.

Tester: `uv run pytest -m "not live"` og `bun test` (krever [bun](https://bun.sh)). Uten
`-m "not live"` kjøres også tre tester mot Brregs ekte API, og to av dem sender en årsrapport
til Gemini når `GEMINI_API_KEY` er satt. Noen tester hoppes over fordi de trenger et ekte
årsregnskap som ikke følger med. `git config core.hooksPath .githooks` slår på en sjekk som
kjører ruff og begge testsettene før hver push.

Lenker rett inn: `/?orgnr=<orgnr>`, `/?eierkart=<orgnr>`, `/?regnskap=<orgnr>&aar=<år>`,
`/?personer=<navn>` og `/?modus=batch`.

## Dette må du tilpasse

**Innlogging.** Appen lager ikke innlogginger selv. Den godtar en `session`-cookie signert med
Ed25519 av en egen innloggingstjeneste, og sender alle andre til `LOGIN_URL`. Hos oss er det en
intern verktøyside (kalt unyttkassa i koden) med Google-innlogging og en liste over godkjente
e-postadresser. Du har to valg:

- Lag en utsteder. Formatet står øverst i `src/session_token.py`: `generate_keypair` lager
  nøkkelparet og `issue_session({"user_id": …, "email": …})` lager cookie-verdien. Utstederen
  har `SESSION_SIGNING_KEY`, Regnskap får bare `SESSION_PUBLIC_KEYS` og `SESSION_ISSUER`.
  Cookien må settes på et domene Regnskap også ligger under.
- Bytt ut `src/auth.py` med det dere bruker fra før. Resten av appen henter `AuthMiddleware`,
  `get_user_email`, `require_admin`, `ADMINS` og `LOGOUT_URL` derfra.

Sett `REGNSKAP_ADMINS` til deres egne adresser. Administratorer kan lese hendelsesloggen og er
unntatt grensene per bruker.

**Aksjonærregisteret.** Eiere, eierkart og personsøk leser `shareholders.db`, som ikke følger
med. Den bygges fra Skatteetatens aksjonærregister, én CSV-fil per år, som dere må skaffe
selv:

```bash
uv run python -m src.shareholders aksjeeiebok_2024.csv aksjeeiebok_2025.csv
```

Filene må hete `aksjeeiebok_<år>.csv`, være semikolonskilt UTF-8 uten anførselstegn og ha
kolonnene `Orgnr;Selskap;Aksjeklasse;Navn aksjonær;Fødselsår/orgnr;Postnr/sted;Landkode;Antall
aksjer;Antall aksjer selskap`, med linjene for hvert selskap samlet. En ny import av et år
erstatter året. Hos oss er årene 2020–2025 til sammen 17 millioner linjer og en database på vel
2 GB. Den legges i datamappen, eller der `SHAREHOLDERS_DB_PATH` peker. Personsøket bygger sin
egen indeks (`people.db`, rundt 150 MB) i bakgrunnen første gang appen starter med et nytt
register. Det tar et par minutter.

Registeret har navn, fødselsår og poststed for alle som eier aksjer i norske selskaper. Avklar
hvem som skal ha tilgang før dere tar det i bruk.

**Språkmodeller og kostnad.** Uten nøkkel gir hver av disse en feilmelding, og resten virker.

| Funksjon | Modell | Kall |
|---|---|---|
| Les inn | `claude-haiku-5-5`, med `gemini-3.5-flash-lite` som reserve og som eneste modell når bare `GEMINI_API_KEY` er satt | to eller tre per side, flere for sider som feiler kontrollene. Hos oss rundt 0,3 øre per side. Hvert kall lagres, så en ny innlesing betaler bare for det som mangler |
| Datterselskaper | `gemini-3.5-flash-lite` | ett per morselskap og regnskapsår, med hele PDF-en. Starter av seg selv første gang noen åpner et morselskap |
| Spør AI | `gemini-3.5-flash-lite` | ett per spørsmål |

Årsregnskapene som sendes til modellene, er offentlige. Chatten sender i tillegg spørsmålet,
selskapets eiere fra aksjonærregisteret (navn og fødselsår) og navnene på brukerens favoritter
og samlinger til Google. Instruksene står i `src/accounts/schema.py`, `src/subsidiaries.py` og
`src/chat.py`, og er skrevet for norske regnskap. Prisene appen regner kostnad ut fra, står i
`src/accounts/llm.py` og må oppdateres når leverandørene endrer dem.

**Grenser.** Per bruker (`src/limits.py`): 60 innleste år per døgn, 20 nye lesinger av
datterselskaper per døgn og 60 spørsmål til AI per time. `READ_BUDGET_USD` er taket for hva
innlesing kan koste per døgn for alle til sammen, også administratorer. ZIP-nedlastinger
(`src/jobs.py`): tre om gangen per bruker, ti totalt, høyst 50 selskaper og 300 filer i hver.
Tellerne per bruker ligger i minnet og nullstilles når appen starter på nytt.

**Kunngjøringer.** Brreg har ikke noe gratis API for kunngjøringer, så `src/kunngjoringer.py`
leser de offentlige sidene på `w2.brreg.no`. Alle brukere deler én takt: høyst to forespørsler
om gangen og minst 0,25 sekunder mellom hver. Et besøk på et selskap henter lista og de fem
nyeste kunngjøringene, resten hentes når brukeren ber om det. Hver kunngjøring leses én gang og
lagres, lista hentes på nytt etter et døgn. Vurder om dette passer hos dere, og si hvem dere
er: `USER_AGENT` i `src/main.py` følger med hvert kall til Brreg.

**Det som lagres.** Alt ligger i datamappen: `regnskap.db`, hver PDF som hentes (`pdf/`, uten
tak) og sidebilder (`pages/`). Hendelsesloggen i `regnskap.db` viser hvem som leste inn hvilket
selskap, og beholdes i 60 dager. Bare administratorer kan lese den, på `/api/diagnostics`.
Eierkart og datterselskaper deles av alle brukere, favoritter og samlinger er personlige.

**Navn og skrift.** Tittelen «Årsregnskap · unyttkassa» står i `src/templates/base.html` og
`src/static/app.js`. Skriften hentes fra Google Fonts, så hver sidevisning går innom Google.

## Miljøvariabler

| Variabel | Brukes til | Standard |
|---|---|---|
| `SESSION_PUBLIC_KEYS`, `SESSION_ISSUER` | verifiserer innloggingscookien | påkrevd i drift |
| `LOGIN_URL`, `LOGOUT_URL` | innloggingstjenestens adresser | `https://example.org/…` |
| `REGNSKAP_ADMINS` | administratorenes e-postadresser, skilt med komma | `admin@example.org` |
| `REGNSKAP_DATA_DIR` | datamappen | `/app/data` hvis den finnes, ellers `./data` |
| `SHAREHOLDERS_DB_PATH` | aksjonærregisteret, når det ligger utenfor datamappen | `shareholders.db` i datamappen |
| `ANTHROPIC_API_KEY` | innlesing av årsregnskap | av |
| `GEMINI_API_KEY` | datterselskaper, chat og reserve for innlesing | av |
| `ACCOUNTS_MODEL`, `ACCOUNTS_FALLBACK` | modell og reserve for innlesing | `claude-haiku-5-5`, `gemini-3.5-flash-lite` |
| `READ_BUDGET_USD` | tak for innlesing per døgn (UTC), i dollar | `20` |
| `JOB_DIR` | der ZIP-filer skrives mens de lages | systemets midlertidige mappe |
| `DEV_INSECURE_AUTH` | `1` skrur av innloggingen lokalt | av |

Bruksmåling (`AI_USAGE_TOKEN`, `AI_USAGE_OUTBOX`, `src/usage.py`, `src/usage_client.py`) sender
tokenforbruk uten innhold til et eget dashbord hos oss. Den er av når variablene mangler. Pek
den mot deres egen mottaker eller slett den.

## Koden

| Fil | Ansvar |
|---|---|
| `src/brreg.py` | klient for Brregs åpne API-er: søk, oppslag, roller, årsliste, PDF, nøkkeltall |
| `src/archive.py` | tar vare på det Brreg sendte, så år Brreg slutter å vise fortsatt vises |
| `src/jobs.py`, `src/naming.py`, `src/parsing.py` | ZIP-jobber, filnavn og org.nr.-kontroll |
| `src/kunngjoringer.py` | kunngjøringer fra `w2.brreg.no` |
| `src/shareholders.py`, `src/register.py`, `src/people.py` | import av aksjonærregisteret, eierstruktur og eierkart, personsøk |
| `src/accounts/` | innlesing av årsregnskap: sidelesing (`llm.py`, `schema.py`), kontroller (`checks.py`), flyt (`pipeline.py`), lagring og søk |
| `src/subsidiaries.py`, `src/chat.py` | datterselskapsnoten og chatten |
| `src/limits.py`, `src/events.py` | grenser per bruker og hendelsesloggen |
| `src/routes/`, `src/db.py` | HTTP-rutene og SQLite-skjemaet |
| `src/static/` | frontend uten byggesteg. `app.js` tegner sidene, de andre modulene er ren logikk og testes med `bun test` |

## Drift

```bash
docker build -t regnskap .
docker run -p 8000:8000 -v regnskap-data:/app/data \
  -e SESSION_PUBLIC_KEYS=… -e SESSION_ISSUER=… -e LOGIN_URL=… -e LOGOUT_URL=… \
  -e REGNSKAP_ADMINS=… -e ANTHROPIC_API_KEY=… -e GEMINI_API_KEY=… \
  regnskap
```

Vi kjører det på [Dokku](https://dokku.com) med et volum montert på `/app/data`:

```bash
ssh dokku@<din-server> apps:create regnskap
ssh dokku@<din-server> git:set regnskap deploy-branch main
ssh dokku@<din-server> storage:ensure-directory regnskap
ssh dokku@<din-server> storage:mount regnskap /var/lib/dokku/data/storage/regnskap:/app/data
ssh dokku@<din-server> config:set regnskap SESSION_PUBLIC_KEYS=<kid>:<nøkkel> \
  SESSION_ISSUER=<utsteder> LOGIN_URL=<innloggingsside> LOGOUT_URL=<utloggingsside> \
  REGNSKAP_ADMINS=<e-post> ANTHROPIC_API_KEY=<nøkkel> GEMINI_API_KEY=<nøkkel>

git remote add dokku dokku@<din-server>:regnskap
git push dokku main
```

Bygg `shareholders.db` lokalt og kopier den til datamappen på serveren. Appen kjører som én
prosess med vilje: nedlastinger og grensene per bruker ligger i minnet. En deploy stopper
innlesinger som er i gang, og den nye prosessen tar dem opp igjen etter et minutts tid.

Appen sjekker `DOKKU_APP_TYPE` for å vite at den er i drift: da nekter den å starte uten
`SESSION_PUBLIC_KEYS` og `SESSION_ISSUER`, og med `DEV_INSECURE_AUTH=1`. På en annen plattform
bør du sette `DOKKU_APP_TYPE=1` selv for å få de samme sperrene.

## Bruk det fritt

Bruk, kopier og endre koden som dere vil. Vi lover ingen støtte, men si gjerne fra om dere tar
den i bruk.

# Kladd

Kladd skriver førsteutkast til norske nyhetsartikler fra kildemateriale: lenker, PDF-er,
intervjuutskrifter og notater. Laget av [Utdanningsnytt](https://www.utdanningsnytt.no), der
redaksjonen bruker det daglig. Koden ligger ute slik at andre redaksjoner kan ta den i bruk og
tilpasse den til sitt eget stoffområde.

Én kodebase, to verktøy. `KLADD_MODE` velger hvilket prosessen kjører:

| | `sitat` (standard) | `manus` |
|---|---|---|
| Bruk | sitatsak fra andre mediers artikler, i NTB-stil | journalistisk sak fra råmateriale |
| Lengde | 110–180 ord | 200–800 ord |
| Navn i grensesnittet | sitatkladd | manuskladd |

Hvert sitat i utkastet sjekkes mot kildene, både replikker med tankestrek og «…» på fire ord
eller mer. Utkastet strømmes til nettleseren mens det skrives, og lagres med kildene sine.

## Kom i gang

Krever [uv](https://docs.astral.sh/uv/) og en Gemini-nøkkel.

```bash
uv sync
export GEMINI_API_KEY=<nøkkel>
uv run python scripts/dev_server.py                   # sitatkladd
KLADD_MODE=manus uv run python scripts/dev_server.py  # manuskladd
```

Åpne `http://127.0.0.1:8000`. `scripts/dev_server.py` starter appen med en engangsnøkkel og
logger deg inn som `dev@example.org`. Det er bare for lokal bruk.

Fra kommandolinja, uten webserver:

```bash
uv run kladd-draft https://example.org/en-artikkel
KLADD_MODE=manus uv run kladd-draft utskrift.md rapport.pdf -o utkast.md
```

Tester: `uv run pytest`.

## Dette må du tilpasse

**Innlogging.** Appen lager ikke innlogginger selv. Den godtar en `session`-cookie signert med
Ed25519 av en egen innloggingstjeneste, og sender alle andre til `LOGIN_URL`. Hos oss er det en
intern verktøyside (kalt unyttkassa i koden) med Google-innlogging og en liste over godkjente
e-postadresser. Du har to valg:

- Lag en utsteder. Formatet står øverst i `kladd/frontend/session_token.py`, og
  `scripts/dev_server.py` viser hele utstedelsen på noen få linjer. Utstederen har
  `SESSION_SIGNING_KEY`, Kladd får bare `SESSION_PUBLIC_KEYS` og `SESSION_ISSUER`. Cookien må
  settes på et domene Kladd også ligger under.
- Bytt ut `kladd/frontend/auth.py` med det dere bruker fra før. Resten av appen trenger bare
  `request.state.user_email` og `request.state.is_admin`.

**Redaksjonelle regler.** De allmenne reglene (kildetroskap, sitattegn, balanse, språk) står i
`libs/unytt_editorial/unytt_editorial/constitution.md`. Format, lengde og sitatsak-reglene står i
`kladd/prompt.py`. Begge er skrevet for vår redaksjon: les dem og skriv om det som ikke passer
hos dere. Faste regler kan også legges inn i appen på `/rules` (bare
adressene i `KLADD_ADMIN_EMAILS`), for alle eller for én bruker.

**Modeller.** `kladd/models.py` setter hovedmodellen (Gemini) og reservekjeden. Reserver uten
nøkkel hoppes over.

**Kildeparsing.** Lenker, PDF, Markdown og ren tekst leses av `libs/unytt_parser`. Den prøver
flere User-Agent-er når et nettsted svarer 403. Vurder om det passer hos dere.

## Miljøvariabler

| Variabel | Brukes til | Standard |
|---|---|---|
| `GEMINI_API_KEY` | utkast og sitatsjekk | påkrevd |
| `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` | reservemodeller når Gemini feiler | av |
| `KLADD_FALLBACK_MODELS` | reservekjeden, kommaseparert | se `kladd/models.py` |
| `KLADD_MODE` | `sitat` eller `manus` | `sitat` |
| `KLADD_DATA_DIR` | katalog for `kladd.sqlite` (utkast, kilder, regler) | `./data` |
| `KLADD_ADMIN_EMAILS` | hvem som får redigere faste regler | `admin@example.org` |
| `SESSION_PUBLIC_KEYS`, `SESSION_ISSUER` | verifiserer innloggingscookien | påkrevd i drift |
| `LOGIN_URL` | dit uinnloggede sendes | `https://example.org/login` |
| `EVALS_API_KEY` | nøkkel for JSON-API-et under `/api/` | påkrevd i drift |
| `KLADD_DEV` | `1` gir en utviklernøkkel for API-et lokalt | av |

Bruksmåling (`AI_USAGE_TOKEN`, `kladd/usage_client.py`) sender tokenforbruk uten innhold til
et eget dashbord hos oss. Den er av når variabelen mangler. Pek den mot deres egen mottaker
eller slett den.

## API

For tjeneste-til-tjeneste-bruk, med `X-API-Key: $EVALS_API_KEY`.

| Metode | Rute | Gjør |
|---|---|---|
| POST | `/api/generate` | lager utkast fra `{"urls": [...]}` |
| GET | `/api/drafts/{id}` | henter et utkast |
| GET | `/api/drafts/{id}/sources` | kildene til et utkast |
| GET | `/api/stats` | antall utkast og tidsbruk |
| GET | `/health` | livstegn, uten innlogging |

## Drift

```bash
docker build -t kladd .
docker run -p 8000:8000 -v kladd-data:/app/data \
  -e KLADD_DATA_DIR=/app/data -e KLADD_MODE=sitat \
  -e GEMINI_API_KEY=… -e EVALS_API_KEY=… \
  -e SESSION_PUBLIC_KEYS=… -e SESSION_ISSUER=… -e LOGIN_URL=… \
  kladd
```

Vi kjører det samme bildet som to [Dokku](https://dokku.com)-apper, én per modus, hver med sitt
datavolum montert på `/app/data`. Appen sjekker `DOKKU_APP_TYPE` for å vite at den er i drift:
da nekter den å starte uten `SESSION_PUBLIC_KEYS`, `SESSION_ISSUER` og en ekte `EVALS_API_KEY`.
På en annen plattform bør du sette `DOKKU_APP_TYPE=1` selv for å få de samme sperrene.

## Bruk det fritt

Bruk, kopier og endre koden som dere vil. Vi lover ingen støtte, men si gjerne fra om dere tar
den i bruk.

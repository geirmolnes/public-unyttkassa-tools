# Radar

Radar leser nyhetsstrømmen for en fagredaksjon og viser hva som er verdt å se på
i dag. Den henter artikler fra RSS-feeder, siler ut det som hører til
redaksjonens felt, lar en språkmodell score nyhetsverdien, og viser resultatet
som en kronologisk feed med søk og filtre.

Laget av Utdanningsnytt, der den dekker barnehage, skole og lærerutdanning. Koden
ligger her slik vi bruker den, altså innstilt på utdanningsfeltet.
[TILPASNING.md](TILPASNING.md) viser steg for steg hva en annen redaksjon må
endre.

## Slik virker den

Hver kjøring (`radar-tick`) gjør fem ting, og gjør ingenting hvis det ikke er
noe nytt:

1. **Innlesing.** Nye og endrede artikler med minst 500 tegn brødtekst, ikke
   eldre enn `MAX_ARTICLE_AGE_DAYS`.
2. **Ordfilter** (`prefilter.py`, `vocab.py`). En billig ordliste som slipper
   gjennom alt som kan være relevant. Rundt 70 prosent faller ut her, og de
   artiklene koster aldri et modellkall.
3. **Språkmodell** (`classify.py`). Gemini svarer med `relevant`, `score` 0–100,
   kategori (`stage`), teksttype (`kind`), emner og en kort begrunnelse.
4. **Forløp** (`cluster.py`). Artikler om samme hendelse knyttes til én sak.
5. **Har vi den?** (`coverage.py`). Valgfritt: sjekker hver sak mot redaksjonens
   eget arkiv.

Feeden viser dagens saker med nyeste øverst og tidligere dager sortert på score.
`/frafiltrert` viser alt som ble kastet og hvorfor.

Artiklene kommer fra en SQLite-fil som `source.py` leser. `radar-fetch` fyller
den fra RSS. Har dere en egen skraper, kan den skrive den samme tabellen i
stedet (kolonnene står øverst i `src/radar/feeds.py`).

## Kjør lokalt

Krever [uv](https://docs.astral.sh/uv/) og en
[Gemini-nøkkel](https://aistudio.google.com/apikey).

```bash
uv sync
cp feeds.example.txt feeds.txt        # bytt ut med egne kilder senere
export GEMINI_API_KEY=<nøkkel>

uv run radar-fetch                    # RSS → artikler, så artikkelsidene
uv run radar-tick --no-llm            # bare ordfilteret, ingen modellkall
uv run radar-tick                     # filter, modell, forløp
AUTH_DISABLED=1 uv run radar-web      # http://127.0.0.1:8770
```

Alt lagres i `./data`. Første `radar-fetch` henter høyst 150 artikkelsider; kjør
den igjen for å hente resten. Første `radar-tick` klassifiserer høyst 200
artikler.

```bash
uv run pytest                         # ingen nett, ingen modell
uv run ruff check src tests
```

## Kommandoer

| Kommando | Gjør |
| --- | --- |
| `radar-fetch` | leser feedene i `feeds.txt` og henter artikkelsidene |
| `radar-tick` | innlesing, ordfilter, modell, forløp, dekning |
| `radar-tick --no-llm` | bare innlesing og ordfilter |
| `radar-tick --reingest` | leser alt i vinduet på nytt, etter endring i ordfilteret |
| `radar-reclassify --stale-prompt` | scorer på nytt etter endret prompt |
| `radar-recluster` | bygger forløpene på nytt |
| `radar-coverage --stale-prompt` | sjekker arkivet på nytt |
| `radar-backup --keep 7` | kopi av databasen til `RADAR_DATA_DIR/backups` |
| `radar-prune` | sletter artikler eldre enn 90 dager |
| `radar-doctor` | sjekker indeks, innstillinger og filer etter deploy |
| `radar-web` | lokal webserver |

## Kildelista

`feeds.txt` har én kilde per linje:

```
# Kildenavn | adresse til RSS- eller Atom-feed
NRK | https://www.nrk.no/toppsaker.rss
Kommunal Rapport | https://www.kommunal-rapport.no/?lab_viewport=rss
```

Kildenavnet er det som vises i feeden og det `kilde:` søker på. Flere feeder kan
dele ett navn. `radar-fetch` henter artikkelsiden én gang per sak og trekker ut
brødteksten med [trafilatura](https://trafilatura.readthedocs.io/). Saker bak
betalingsmur gir bare ingressen og faller ut på 500-tegnsgrensen. Sjekk vilkårene
til kildene dere leser, og sett `RADAR_USER_AGENT` til noe som sier hvem dere er.

## Innstillinger

| Variabel | Brukes til | Standard |
| --- | --- | --- |
| `GEMINI_API_KEY` | modellkallene | påkrevd |
| `GEMINI_MODEL` | modell | `gemini-3.5-flash-lite` |
| `RADAR_DATA_DIR` | Radars egen database og sikkerhetskopier | `./data` |
| `SCRAPER_DB_PATH` | artikkelbasen `radar-fetch` skriver og `radar-tick` leser | `./data/news_scraper.db` |
| `RADAR_FEEDS_FILE` | kildelista | `feeds.txt` |
| `RADAR_USER_AGENT` | hvem `radar-fetch` sier den er | `radar-rss/0.1 (newsroom RSS reader)` |
| `FETCH_BODY_LIMIT` | artikkelsider hentet per `radar-fetch` | `150` |
| `SEED_WINDOW_DAYS` | hvor langt tilbake første kjøring går | `7` |
| `MAX_ARTICLE_AGE_DAYS` | eldste artikkel som leses inn | `14` |
| `CLASSIFY_LIMIT` | artikler klassifisert per kjøring, `0` tømmer køen | `200` |
| `ARCHIVE_DB_PATH` | eget arkiv for «Har vi den?» (valgfritt) | `./data/unytt_artikler.db` |
| `SESSION_PUBLIC_KEYS`, `SESSION_ISSUER` | innlogging, se under | påkrevd på Dokku |
| `LOGIN_URL` | dit uinnloggede sendes | `https://example.org/login` |
| `AUTH_DISABLED` | `1` skrur av innlogging lokalt | av |
| `SITATKLADD_URL` | hvor knappen «Lag sitatsak» sender saken | se TILPASNING.md |
| `UNYTTKASSA_URL` | lenken øverst i menyen | se TILPASNING.md |

Resten (grenser for forløp og dekning, tekstlengder) står med begrunnelse i
`src/radar/config.py`.

Valgfritt, og av når variablene mangler: `STATS_TOKEN` åpner `GET /stats` for
overvåking, `EVALS_API_KEY` åpner `GET /api/stats` med driftsstatus, og
`AI_USAGE_*` sender tokenforbruk til en egen mottaker (`usage_client.py`).

## Innlogging

Radar har ingen egen innlogging. Den godtar en `session`-cookie som er signert
med Ed25519 av en innloggingstjeneste vi kjører selv, og sender alle uten gyldig
cookie til `LOGIN_URL`. Radar har bare den offentlige
nøkkelen: den kan sjekke en innlogging, ikke lage en. Alle med gyldig cookie
slipper inn, så hvem som får logge inn avgjøres av tjenesten som utsteder den.

Dere må velge én av to:

- **Lag en utsteder.** Formatet og koden står i
  `src/radar/web/session_token.py`: `generate_keypair` lager nøkkelparet,
  `issue_session({"user_id": …, "email": …})` lager cookie-verdien. Tjenesten
  deres setter den som `session` på et domene Radar også ligger under. Radar får
  `SESSION_PUBLIC_KEYS` og `SESSION_ISSUER`, og `LOGIN_URL` peker på
  innloggingssiden.
- **Bytt ut `web/auth.py`** med det dere allerede har. `web/app.py` henter
  `AuthMiddleware`, `auth_disabled`, `current_user` og `require_stats_token`
  derfra. `current_user(request)` må gi `{"id", "email"}`, som brukes til «Jeg
  tar denne» og tommel opp/ned.

`AUTH_DISABLED=1` er bare for lokal kjøring. På Dokku nekter appen å starte med
den, og uten `SESSION_PUBLIC_KEYS` og `SESSION_ISSUER`.

## Har vi den? (valgfritt)

Radar kan merke saker redaksjonen allerede har skrevet om. Den leser en SQLite-fil
(`ARCHIVE_DB_PATH`) med redaksjonens egne artikler, som dere må fylle selv fra
publiseringssystemet:

```sql
CREATE TABLE articles (
    article_id INTEGER PRIMARY KEY,
    url TEXT, title TEXT, lead TEXT, body_text TEXT,
    keywords TEXT,            -- fritekst, brukes til å rangere kandidater
    article_section TEXT,     -- seksjon, se NOT_REPORTING i archive.py
    date_published TEXT       -- ISO 8601 i UTC, f.eks. 2026-11-16T08:00:00.000Z
);
CREATE TABLE external_links (
    url TEXT,                 -- en lenke i artikkelen
    article_id INTEGER        -- artikkelen den står i
);
```

En lenke fra en egen artikkel til nøyaktig den adressen Radar har, gir merket
«Sitert av oss» uten modellkall. Resten avgjør modellen ut fra tittel, ingress
og brødtekst.

Uten arkivet hopper steget over seg selv, men feeden viser da en gul stripe om at
«Har vi den?» er ubesvart. TILPASNING.md sier hvordan den fjernes.

## Deploy

### Docker

```bash
docker build -t radar .
docker run -p 8000:8000 -v radar-data:/app/data \
  -e GEMINI_API_KEY=<nøkkel> \
  -e SESSION_PUBLIC_KEYS=<kid>:<nøkkel> -e SESSION_ISSUER=<utsteder> -e LOGIN_URL=<innloggingsside> \
  radar
```

`feeds.txt` kopieres inn i bildet fra repoet. Alt appen skriver, ligger i
`/app/data`. Har dere en egen skraper og trenger ikke RSS-leseren, bygg med
`--build-arg RADAR_EXTRAS=""`. Jobbene må dere planlegge selv, for eksempel `docker exec <container> radar-fetch` og
`docker exec <container> radar-tick` fra cron på verten.

### Dokku

```bash
ssh dokku@<din-server> apps:create radar
ssh dokku@<din-server> git:set radar deploy-branch main
ssh dokku@<din-server> domains:add radar radar.<ditt-domene>
ssh dokku@<din-server> storage:ensure-directory radar
ssh dokku@<din-server> storage:mount radar /var/lib/dokku/data/storage/radar:/app/data
ssh dokku@<din-server> config:set radar GEMINI_API_KEY=<nøkkel> \
  SESSION_PUBLIC_KEYS=<kid>:<nøkkel> SESSION_ISSUER=<utsteder> LOGIN_URL=<innloggingsside>

git remote add dokku dokku@<din-server>:radar
git push dokku main
```

`/health` svarer 503 til artikkelbasen finnes, så den aller første deployen
stopper på helsesjekken. Hopp over sjekken den ene gangen, kjør første
innhenting og slå den på igjen:

```bash
ssh dokku@<din-server> checks:skip radar
git push dokku main
ssh dokku@<din-server> run radar radar-fetch
ssh dokku@<din-server> run radar radar-tick
ssh dokku@<din-server> checks:enable radar
```

`app.json` setter opp fire faste jobber: `radar-fetch` og `radar-tick` hver
halvtime, `radar-backup` hver natt og `radar-prune` hver søndag.

Sikkerhetskopiene ligger på samme disk som databasen. Ta kopi av
`RADAR_DATA_DIR` et annet sted også.

## Bruk

Fritt fram: bruk, kopier og endre koden som dere vil. Vi lover ingen støtte, men
si gjerne fra om feil.

Skriftene i `src/radar/web/static/fonts` er andres verk, se `LISENS.md` i den
katalogen.

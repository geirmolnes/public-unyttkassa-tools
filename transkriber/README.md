# Transkriber

Transkriber gjør lydopptak om til tekst for journalister: last opp en lydfil, få et transkript
med talere og klikkbare tidsstempler. Laget av [Utdanningsnytt](https://www.utdanningsnytt.no),
der redaksjonen bruker det daglig. Koden ligger ute slik at andre redaksjoner kan ta den i bruk
og tilpasse den.

- Transkribering med [ElevenLabs Scribe](https://elevenlabs.io/speech-to-text), med talerskille.
  Flere timers opptak går i én jobb.
- To etterpass med Gemini: spørsmål merkes, og en renset versjon uten fyllord lages ved siden
  av råteksten.
- Klikk på et tidsstempel for å høre stedet. Klipp ut lyd, gi talerne navn, eksporter til
  tekst, Word, SRT eller VTT.
- Hver bruker ser bare sine egne opptak.

## Kom i gang

Krever [uv](https://docs.astral.sh/uv/), `ffmpeg`, en ElevenLabs-nøkkel og en Gemini-nøkkel.

```bash
uv sync
export ELEVENLABS_API_KEY=<nøkkel>
export GEMINI_API_KEY=<nøkkel>
uv run python scripts/dev_server.py
```

Åpne `http://127.0.0.1:8000`. `scripts/dev_server.py` starter appen med en engangsnøkkel og
logger deg inn som `dev@example.org`. Det er bare for lokal bruk.

Tester: `uv run pytest` og `bun install && bun test tests/`. `tests/test_e2e.py` kjører bare
når `ELEVENLABS_API_KEY` er satt, og sender da et syntetisk opptak til Scribe.

## Dette må du tilpasse

**Innlogging.** Appen lager ikke innlogginger selv. Den godtar en `session`-cookie signert med
Ed25519 av en egen innloggingstjeneste, og sender alle andre til `LOGIN_URL`. Hos oss er det en
intern verktøyside (kalt unyttkassa i koden) med Google-innlogging og en liste over godkjente
e-postadresser. Du har to valg:

- Lag en utsteder. Formatet står øverst i `src/session_token.py`, og `scripts/dev_server.py`
  viser hele utstedelsen på noen få linjer. Utstederen har `SESSION_SIGNING_KEY`, Transkriber
  får bare `SESSION_PUBLIC_KEYS` og `SESSION_ISSUER`. Cookien må settes på et domene Transkriber
  også ligger under.
- Bytt ut `src/auth.py` med det dere bruker fra før. Resten av appen trenger bare
  `request.state.user_email`.

**Kildevern.** Lydfilene sendes til ElevenLabs og teksten til Google. Avklar databehandleravtaler
og hva redaksjonen kan laste opp før dere tar det i bruk. Opptak ligger på disk i `UPLOAD_DIR`
til brukeren sletter dem, eller til `AUDIO_RETENTION_DAYS` har gått. Teksten beholdes.

**Etterpassene.** Instruksene for spørsmålsmerking og rensing står i `src/prompt.py` og er
skrevet for norsk tale.

## Miljøvariabler

| Variabel | Brukes til | Standard |
|---|---|---|
| `ELEVENLABS_API_KEY` | transkribering | påkrevd |
| `GEMINI_API_KEY` | spørsmålsmerking og rensing. Appen starter ikke uten, men et etterpass som feiler stopper ikke transkriberingen | påkrevd |
| `GEMINI_MODEL`, `CLEANUP_MODEL` | modell for hvert etterpass | se `src/transcribe.py` |
| `ELEVENLABS_MODEL_ID` | Scribe-modell | `scribe_v2` |
| `UPLOAD_DIR` | der lydfilene ligger | `./uploads` |
| `TRANSKRIBER_DB` | SQLite-fila med jobber og transkripter | `uploads/transkriber.db` |
| `MAX_UPLOAD_MB` | største opplasting | `2048` |
| `TRANSCRIBE_TIMEOUT` | maks sekunder for én jobb | `2700` |
| `AUDIO_RETENTION_DAYS` | dager før lydfiler slettes, `0` = aldri | `0` |
| `SESSION_PUBLIC_KEYS`, `SESSION_ISSUER` | verifiserer innloggingscookien | påkrevd i drift |
| `LOGIN_URL`, `LOGOUT_URL` | innloggingstjenestens adresser | `https://example.org/…` |
| `EVALS_API_KEY` | nøkkel for `/api/stats`, av når den mangler | av |

Bruksmåling (`AI_USAGE_TOKEN`, `src/usage_client.py`, `src/usage_sdk.py`) sender tokenforbruk
uten innhold til et eget dashbord hos oss. Den er av når variabelen mangler. Pek den mot deres
egen mottaker eller slett den.

## Drift

```bash
docker build -t transkriber .
docker run -p 5000:5000 -v transkriber-uploads:/app/uploads \
  -e ELEVENLABS_API_KEY=… -e GEMINI_API_KEY=… \
  -e SESSION_PUBLIC_KEYS=… -e SESSION_ISSUER=… -e LOGIN_URL=… -e LOGOUT_URL=… \
  transkriber
```

Vi kjører det på [Dokku](https://dokku.com) med et volum montert på `/app/uploads`. Sett
proxyens grense for opplastinger over `MAX_UPLOAD_MB`, på Dokku med
`dokku nginx:set <app> client-max-body-size 2100m`. Appen sjekker `DOKKU_APP_TYPE` for å vite at
den er i drift: da nekter den å starte uten `SESSION_PUBLIC_KEYS` og `SESSION_ISSUER`. På en
annen plattform bør du sette `DOKKU_APP_TYPE=1` selv for å få den samme sperren.

Tapsfrie opplastinger (WAV, AIFF, FLAC) komprimeres til mono MP3 etter opplasting. Støttede
formater: MP3, WAV, M4A, MP4, OGG, FLAC, AAC og AIFF.

## Bruk det fritt

Bruk, kopier og endre koden som dere vil. Vi lover ingen støtte, men si gjerne fra om dere tar
den i bruk.

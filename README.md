# Verktøy fra unyttkassa

Redaksjonen i [Utdanningsnytt](https://www.utdanningsnytt.no) har en intern verktøykasse,
unyttkassa. Her ligger koden til verktøyene, slik at andre redaksjoner kan ta dem i bruk og
tilpasse dem til sitt eget stoffområde.

| Mappe | Verktøy |
|---|---|
| [`radar/`](radar/) | Løpende nyhetsfeed fra et fagfelt, sortert etter nyhetsverdi. |
| [`kladd/`](kladd/) | Lager utkast til sitatsaker fra lenker og dokumenter. |
| [`transkriber/`](transkriber/) | Gjør lydopptak om til tekst med klikkbare tidsstempler. |

Hver mappe er et selvstendig prosjekt med egen README, egne tester og egen `Dockerfile`.
README-ene går ut fra at prosjektet ligger i roten av et repo. Kopier mappa til deres eget repo
før dere setter det i drift.

## Felles for alle

- **Koden er slik vi bruker den.** Den er innstilt på vår redaksjon og vårt fagfelt. README-en i
  hver mappe sier hva dere må tilpasse. For Radar er det en egen guide, `radar/TILPASNING.md`.
- **Innlogging må dere ordne selv.** Verktøyene lager ikke innlogginger. De godtar en signert
  cookie fra en innloggingstjeneste vi kjører internt, og hver README forklarer hvordan dere lager
  en egen utsteder eller bytter ut den ene filen som sjekker innloggingen. Alle kan kjøres lokalt
  uten.
- **Dere trenger egne API-nøkler** til språkmodellene (Gemini, og ElevenLabs for Transkriber).
- **Øyeblikksbilder, ikke utviklingshistorikk.** Repoet oppdateres når vi har noe nytt å dele.

## Bruk det fritt

Bruk, kopier og endre koden som dere vil. Vi lover ingen støtte, men si gjerne fra om dere tar
den i bruk.

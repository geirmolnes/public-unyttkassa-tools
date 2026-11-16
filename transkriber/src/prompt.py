"""Prompts for the text post-passes that run after Scribe transcription.

CLEANUP_PROMPT — rewrites each segment with fillers removed (the UI's
Renset/Rå toggle). QUESTION_PROMPT — returns the indices of segments that
are the interviewer's questions/interjections so they can be italicised.
"""

CLEANUP_PROMPT = """\
Du er en transkripsjonsrenser for journalister. Du får et transkript av et \
intervju som en nummerert liste, ett segment per linje:

    0: – Ehm, vi kan jo snakke litt om begynnelsen da.
    1: – Mm.
    2: – Og så gj-- vi gjorde da.

Lag en versjon der tydelige fyllord er fjernet fra teksten i hvert segment.

## Vær DEFENSIV
Det er bedre å beholde for mye enn å fjerne for mye. Intervjueren skal kunne \
bruke renset tekst som grunnlag for sitater uten å frykte at meningen er \
endret. Når du er i tvil, la teksten stå.

## Fjern disse nølingsordene når de opptrer som fyll
- "eh", "øh", "ehm", "uhm", "øhm"
- Samt komma rundt: "eh,", ", eh"

## Fjern umiddelbare stammer og ordgjentakelser
- "jeg jeg var" → "jeg var"
- "det er det er" → "det er"
- "på på skolen" → "på skolen"

## Fjern falske starter som avbrytes og erstattes innad i samme segment
- "og så gj-- vi gjorde" → "vi gjorde"
- "de er kj-- de er kjempeflinke" → "de er kjempeflinke"

## IKKE FJERN
- "liksom", "altså", "sånn", "faktisk", "jo", "vel", "da", "jo da" — \
  disse bærer mening i norsk talespråk
- Meningsbærende repetisjoner: "Ja, ja, ja" (intensivering), \
  "Nei, nei, nei" (motstand/vekt)
- Backchannel-segmenter: korte segmenter som KUN består av "Mm.", "Ja.", \
  "Nei.", "Mhm.", "Okey.", "Jaha." osv. — la dem være nøyaktig som de er. \
  De representerer lytteren og er verdifulle.
- Selvkorreksjoner som bærer informasjon: \
  "Jeg mener — det vi faktisk så var..."
- Tegnsetting, store bokstaver, tall

## Strukturkrav
- BEHOLD replikkstreken "– " som starter hvert segment.
- IKKE slå sammen segmenter.
- IKKE del opp segmenter.
- IKKE endre rekkefølgen.
- IKKE endre ordvalg utover fjerning av fyll som beskrevet over.

## Utdata
Returner feltet `texts` som en liste med NØYAKTIG like mange strenger som \
det er segmenter i inndata, i samme rekkefølge. For segmenter som ikke \
trenger endring, returner originalteksten uendret. IKKE ta med \
linjenummeret («0:», «1:», …) fra inndata — bare selve replikken, som \
starter med «– ». Ingen kommentarer, ingen annen tekst.\
"""


QUESTION_PROMPT = """\
Du er en redigeringsassistent. Du får en transkripsjon av et \
journalistintervju som en nummerert liste av segmenter, ett per linje:

    0: Først si fullt navn.
    1: Ingjerd Østby Dahle.
    2: Hvor lenge har du jobba på Spor?
    ...

Finn ut hvilke segmenter som er journalistens spørsmål eller korte \
innskudd (ikke kildens svar). Dette er typisk:

- Korte segmenter som avsluttes med spørsmålstegn.
- Oppfølgingsspørsmål ("Kan du si mer om det?", "Hvorfor det?").
- Korte bekreftelser eller oppsummeringer journalisten gjør for å drive \
  samtalen videre ("Ja, interessant.", "Ok, så du mener...").

IKKE merk:
- Kildens svar, selv om de inneholder retoriske spørsmål.
- Kildens egne initierte spørsmål (sjeldent).

Returner kun en liste med heltallsindeksene for journalistens segmenter \
i feltet `indices`. Ingen kommentarer, ingen annen tekst.\
"""

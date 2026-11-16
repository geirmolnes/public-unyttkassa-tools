"""Page-read schemas and prompts, as benchmarked in brreg_parse_bench (page read p6, number list, text t2)."""

from typing import Literal

from pydantic import BaseModel

PAGE_PROMPT_VERSION = "p6"
TEXT_PROMPT_VERSION = "t2"

Key = Literal[
    "sum_driftsinntekter",
    "lonnskostnad",
    "sum_driftskostnad",
    "driftsresultat",
    "sum_finansinntekter",
    "sum_finanskostnad",
    "netto_finans",
    "res_for_skatt",
    "skatt",
    "aarsresultat",
    "sum_anleggsmidler",
    "sum_omlopsmidler",
    "sum_eiendeler",
    "sum_innskutt_ek",
    "sum_opptjent_ek",
    "sum_ek",
    "sum_langsiktig_gjeld",
    "sum_kortsiktig_gjeld",
    "sum_gjeld",
    "sum_ek_gjeld",
]


class Line(BaseModel):
    label: str
    note_ref: str | None
    key: Key | None
    current: int | None
    prior: int | None


class NoteRow(BaseModel):
    label: str
    role: Literal["item", "total", "other"]
    key: Key | None
    values: list[int | None]


class NoteTable(BaseModel):
    columns: list[str]
    rows: list[NoteRow]


class Note(BaseModel):
    number: str | None
    title: str
    tables: list[NoteTable]


class InlineAmount(BaseModel):
    context: str
    value: int


class NumberList(BaseModel):
    numbers: list[int]


class Page(BaseModel):
    page_kind: Literal[
        "resultat", "balanse", "kontantstrom", "noter", "beretning", "revisjon", "annet"
    ]
    is_brreg_printout: bool
    figures_for: Literal["selskap", "konsern", "annen_enhet", "ingen"]
    unit_scale: int
    current_year: int | None
    prior_year: int | None
    statement_lines: list[Line]
    notes: list[Note]
    inline_amounts: list[InlineAmount] = []


class TextBlock(BaseModel):
    note: str | None
    kind: Literal["heading", "paragraph", "list_item", "table"]
    text: str


class PageText(BaseModel):
    blocks: list[TextBlock]


CONTEXT = (
    "This filing is the årsregnskap of {company}, org.nr {orgnr}, for financial year {year}.\n\n"
)

PAGE_PROMPT = """This is one scanned page from a Norwegian annual report (årsregnskap). Extract it as JSON.

Rules:
- Numbers: integers exactly as printed (no rescaling). Parentheses or a leading minus mean negative. Empty or '-' → null.
  Amounts printed with decimals (øre, e.g. '110 422 871,00') → whole kroner; drop the decimals, never append them.
- unit_scale: 1000 if amounts are stated in thousands (e.g. 'NOK 1000', 'TNOK', 'hele tusen'), 1000000 for millions, else 1.
- statement_lines: every line of a resultatregnskap / balanse / kontantstrøm on this page, in order.
  current = the most recent year's column, prior = the previous year's column. note_ref = the note column text, if any.
  key = the matching canonical key only if the line IS that total (e.g. 'Sum eiendeler' → sum_eiendeler,
  'Ordinært resultat før skattekostnad' → res_for_skatt, 'Skattekostnad på ordinært resultat' → skatt,
  'Lønnskostnad' → lonnskostnad); otherwise null.
  Only the overall total line gets a key. Sub-totals and components never do: 'Sum annen langsiktig gjeld' and
  'Sum avsetninger for forpliktelser' are NOT sum_langsiktig_gjeld; 'Aksjekapital'/'Share capital' is NOT sum_innskutt_ek.
  If the page has no overall total line for a key, do not use that key.
- notes: every note on this page. For each table: column headers, and each row with its values left to right.
  role = 'total' if the row is computed from the rows directly above it since the previous total or table start
  (a sum, a roll-forward like opening balance + additions − disposals, or a difference like cost − accumulated depreciation);
  'item' for every row it is computed from (including an opening balance); 'other' for anything else (ratios, text, memo lines).
  key = canonical key if the row states the same figure as that statement total (e.g. equity at year end → sum_ek,
  total tax cost → skatt, total salary cost → lonnskostnad); otherwise null.
  Only set a note row key when the note restates that exact statement figure (equity note closing total, tax note total
  tax cost, salary note total). Never set keys in notes about pledged assets / secured
  debt (pant), guarantees, debt maturity, interest-bearing debt or related parties, even if a row is called 'Sum ... gjeld'.
- Leave statement_lines / notes empty when the page has none. Do not invent rows.
- page_kind: resultat (income statement), balanse (balance sheet), kontantstrom, noter (notes, incl. accounting
  principles), beretning (board's report / årsberetning), revisjon (auditor's report), annet (cover, signatures,
  forms, other). The page may be part of any section of the filing.
- is_brreg_printout: true only for Brønnøysundregistrene's own typed printout of the submitted figures
  (typewriter font, footer 'Utskriftsdato … Organisasjonsnr … Side x av y'); false for the company's own pages.
- Accounts may be in English (Income statement, Balance sheet, Total assets, Total equity, Notes): use the same keys.
- The scan may be rotated or upside down: read it in its correct orientation.
- figures_for: whose figures this page shows — 'selskap' for {company}'s own (company-level) accounts,
  'konsern' for the consolidated group accounts of {company}, 'annen_enhet' for any other entity (e.g. a parent
  company's or group's own report bundled into the filing, a subsidiary), 'ingen' if the page has no figures.
- Completeness: include EVERY table on the page, however small (e.g. a short tax calculation above a larger table,
  a shareholder list, a one-row table), and every row including total rows without a label.
- inline_amounts: every number written in running text or as a single labelled line outside any table
  (e.g. 'bundne midler kr 11 511', 'Antall årsverk: 11', 'honorar til revisor kr 31 000'), with a short context.
  Not years, dates, page numbers, org.nr or note references."""

NUMLIST_PROMPT = """List every number printed on this scanned page, once per occurrence, as integers.
Include amounts in tables and in running text, counts and totals. Thousands separators (space or dot) are part of one
number: '1 234 567' → 1234567. A comma followed by one or two digits is a decimal mark, not a thousands separator:
'638,8' → 638, '22,06' → 22, '110 422 871,00' → 110422871 (drop the decimals). Parentheses or minus → negative.
Write numbers as printed: 'NOK 5 millioner' → 5.
Exclude: years, dates, page numbers, organisation numbers, phone numbers, account numbers, postal codes, street
numbers, note reference numbers, percentages, and references to standards or laws (IFRS 15, IAS 7, § 3-3).
Return only the list."""

TEXT_PROMPT = """This is one scanned page from the årsregnskap of {company} (org.nr {orgnr}) for {year}.
Transcribe the running text on the page, verbatim, in reading order.

Rules:
- One block per heading, paragraph or bullet point. kind = heading (note titles and sub-headings), paragraph,
  list_item, table (see below).
- Copy the words exactly as printed: same language, spelling, numbers, punctuation and capitalisation. Do not
  summarise, translate, correct or complete anything. Join lines that the layout broke mid-sentence; rejoin words
  hyphenated only because of a line break.
- Do not transcribe tables (rows and columns of figures, including their headers). Where a table sits, add one block
  kind = table with text = the table's first row label, so the order of text and tables is kept.
- Skip statement lines, page headers and footers,
  page numbers, Brønnøysundregistrene's printout footer, signature lines, names under signatures and stamp or
  e-signature codes.
- note: the number of the note the block belongs to, as printed in its heading ('Note 3 Lønnskostnader' → '3').
  Text before the first note heading on the page (a note continued from the previous page) → null.
  Text outside the notes (board's report, auditor's report, cover) → null.
- A page with no running text → empty list."""

# Re-read hints (repair). Worded as uncertain: Haiku follows a confident wrong hint and invents rows.
ALIGN_HINT = (
    "Amounts on this page may be printed offset from their labels (skewed or misaligned scan, amounts slightly "
    "above or below their text line). Assign each amount to the row it belongs to using the column and "
    "row structure of the statement, not just the text line it happens to sit on. Do not change any number."
)
FILL_HINT = (
    "An automatic number list suggests these numbers may be printed on this page but missing from a previous "
    "extraction of it: {nums}. The list can contain mistakes. For each one you can actually see printed on the "
    "page, include the statement line, table row or inline amount it belongs to, with its own label and "
    "column. Ignore any number you cannot find on the page. Do not attach a number to a row it is not printed "
    "on, and do not change other values."
)


def page_prompt(company: str, orgnr: str, year: int, hint: str | None = None) -> str:
    """The p6 page prompt, told whose filing it is (so it can tell own accounts from bundled reports)."""
    base = CONTEXT.format(company=company, orgnr=orgnr, year=year) + PAGE_PROMPT.replace(
        "{company}", company
    )
    return f"{base}\n\n{hint}" if hint else base


def text_prompt(company: str, orgnr: str, year: int) -> str:
    return TEXT_PROMPT.format(company=company, orgnr=orgnr, year=year)

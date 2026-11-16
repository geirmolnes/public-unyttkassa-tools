// Key figures across years without DOM: row definitions and export sheets.
// Rendered by app.js, tested with `bun test`.

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

export const taxOf = (f) => (isNum(f.skattekostnad) ? f.skattekostnad : f.skattekostnad_avledet);
const sumEkGjeld = (f) => (isNum(f.egenkapital) && isNum(f.sum_gjeld) ? f.egenkapital + f.sum_gjeld : f.sum_eiendeler);

// [label, getter, kind, sign, optional]. sign -1 = cost, shown negated (tax income then shows positive);
// 0 = ratio, shown as percent. "sub" and optional rows only show
// when some year has a value.
export const RESULT_ROWS = [
  ["Driftsinntekter", (f) => f.driftsinntekter, "", 1],
  ["herav salgsinntekter", (f) => f.salgsinntekter, "sub", 1],
  ["Driftskostnader", (f) => f.driftskostnader, "", -1],
  ["herav lønn", (f) => f.loennskostnad, "sub", -1],
  ["Driftsresultat", (f) => f.driftsresultat, "sum", 1],
  ["Finansinntekter", (f) => f.finansinntekter, "", 1],
  ["Finanskostnader", (f) => f.finanskostnader, "", -1],
  ["herav rente til selskap i konsernet", (f) => f.rentekostnad_konsern, "sub", -1],
  ["herav annen rentekostnad", (f) => f.rentekostnader, "sub", -1],
  ["Netto finans", (f) => f.netto_finans, "sum", 1, true],
  ["Resultat før skatt", (f) => f.resultat_for_skatt, "sum", 1],
  ["Skattekostnad", taxOf, "", -1],
  ["Ekstraordinære poster", (f) => f.ekstraordinaere_poster, "", 1, true],
  ["Skatt på ekstraordinært resultat", (f) => f.skatt_ekstraordinaert, "", -1, true],
  ["Årsresultat", (f) => f.aarsresultat, "total", 1],
  ["Totalresultat", (f) => f.totalresultat, "", 1, true],
];
export const BALANCE_ROWS = [
  ["Anleggsmidler", (f) => f.anleggsmidler, "", 1],
  ["herav goodwill", (f) => f.goodwill, "sub", 1],
  ["Omløpsmidler", (f) => f.omlopsmidler, "", 1],
  ["herav varer", (f) => f.varer, "sub", 1],
  ["herav fordringer", (f) => f.fordringer, "sub", 1],
  ["herav investeringer", (f) => f.investeringer, "sub", 1],
  ["herav bankinnskudd", (f) => f.bankinnskudd, "sub", 1],
  ["Sum eiendeler", (f) => f.sum_eiendeler, "sum", 1],
  ["Egenkapital", (f) => f.egenkapital, "", 1],
  ["Langsiktig gjeld", (f) => f.langsiktig_gjeld, "", 1],
  ["Kortsiktig gjeld", (f) => f.kortsiktig_gjeld, "", 1],
  ["Sum gjeld", (f) => f.sum_gjeld, "sum", 1],
  ["Sum EK og gjeld", sumEkGjeld, "total", 1],
];
export const EK_ROW = ["EK-andel", (f) => f.ek_andel, "", 0];

/** Short label for the years Brreg has as data, newest-first history: "2023–25". */
export function spanLabel(history) {
  const first = history[history.length - 1].year;
  const last = history[0].year;
  if (first === last) return String(first);
  const sameCentury = Math.floor(first / 100) === Math.floor(last / 100);
  return `${first}–${sameCentury ? String(last).slice(2) : last}`;
}

/** Rows to show for these years: "sub" and optional rows only when some year has a value. */
export const visibleRows = (rows, years) =>
  rows.filter(([, get, kind, , optional]) => (kind !== "sub" && !optional) || years.some((f) => isNum(get(f))));

const EXPORT_EQUITY = [
  ["herav innskutt egenkapital", (f) => f.innskutt_egenkapital, "sub", 1],
  ["herav opptjent egenkapital", (f) => f.opptjent_egenkapital, "sub", 1],
];

/**
 * The lines on screen as two sheets, Resultat and Balanse: a header row "Post (<currency>)" + years
 * (oldest first), then one row per line in whole units, costs negative as shown, EK-andel in percent
 * with one decimal, null where a year lacks the line. bold = sum/total rows, indent = "herav" rows
 * (row indices, header = 0). history is newest first.
 */
export function exportSheets(history) {
  const ys = history.slice().reverse();
  const head = [`Post (${history[0]?.currency || "NOK"})`, ...ys.map((f) => f.year)];
  const balance = BALANCE_ROWS.flatMap((r) => (r[0] === "Egenkapital" ? [r, ...EXPORT_EQUITY] : [r]));
  const value = (get, sign, f) => {
    const v = get(f);
    if (!isNum(v)) return null;
    if (sign === 0) return Math.round(v * 1000) / 10;
    return Math.round(sign < 0 ? -v : v);
  };
  return [["Resultat", RESULT_ROWS], ["Balanse", [...balance, ["EK-andel (%)", EK_ROW[1], "", 0]]]].map(([name, rows]) => {
    const shown = visibleRows(rows, ys);
    return {
      name,
      rows: [head, ...shown.map(([label, get, , sign]) => [label, ...ys.map((f) => value(get, sign, f))])],
      bold: shown.flatMap(([, , kind], i) => (kind === "sum" || kind === "total" ? [i + 1] : [])),
      indent: shown.flatMap(([, , kind], i) => (kind === "sub" ? [i + 1] : [])),
    };
  });
}

/**
 * Both sheets as tab-separated text for pasting into Google Sheets or Excel: the sheet name, its
 * rows, a blank line between. Decimals use a comma (Norwegian locale), empty cells stay empty.
 */
export function toTsv(sheets) {
  const cell = (v) => (v === null ? "" : typeof v === "number" ? String(v).replace(".", ",") : String(v).replace(/[\t\r\n]+/g, " "));
  return sheets.map((s) => [s.name, ...s.rows.map((r) => r.map(cell).join("\t"))].join("\n")).join("\n\n") + "\n";
}

/** E.g. "HJR_HOLDING_AS_konsernregnskap_2023-2025.xlsx" (scope "selskap" | "konsern"), PDF name rules. */
export function exportFilename(name, scope, history, ext = "xlsx") {
  const kind = scope === "konsern" ? "konsernregnskap" : "selskapsregnskap";
  const s = (name || "").normalize("NFC").trim()
    .replace(/[\s/\\]+/g, "_").replace(/[^\p{L}\p{N}_-]/gu, "").replace(/_{2,}/g, "_")
    .replace(/^[_-]+|[_-]+$/g, "").slice(0, 80).replace(/[_-]+$/, "") || "selskap";
  const lo = history[history.length - 1].year, hi = history[0].year;
  return `${s}_${kind}_${lo === hi ? lo : `${lo}-${hi}`}.${ext}`;
}

// ---------- years read from the PDF (GET /api/company/{orgnr}/accounts/figures) ----------

/** Row label → key-figure field, for looking up a figure's sources. Computed rows (Sum EK og gjeld) have none. */
export const FIELD = {
  "Driftsinntekter": "driftsinntekter", "herav salgsinntekter": "salgsinntekter", "Driftskostnader": "driftskostnader",
  "herav lønn": "loennskostnad", "Driftsresultat": "driftsresultat", "Finansinntekter": "finansinntekter",
  "Finanskostnader": "finanskostnader", "herav rente til selskap i konsernet": "rentekostnad_konsern",
  "herav annen rentekostnad": "rentekostnader", "Netto finans": "netto_finans", "Resultat før skatt": "resultat_for_skatt",
  "Skattekostnad": "skattekostnad", "Årsresultat": "aarsresultat", "Anleggsmidler": "anleggsmidler",
  "herav goodwill": "goodwill", "Omløpsmidler": "omlopsmidler", "herav varer": "varer", "herav fordringer": "fordringer",
  "herav investeringer": "investeringer", "herav bankinnskudd": "bankinnskudd", "Sum eiendeler": "sum_eiendeler",
  "Egenkapital": "egenkapital", "Langsiktig gjeld": "langsiktig_gjeld", "Kortsiktig gjeld": "kortsiktig_gjeld",
  "Sum gjeld": "sum_gjeld",
};

/** Brreg's years plus the years read from PDFs that Brreg lacks, newest first. */
export function mergeHistory(brreg, read) {
  const have = new Set((brreg || []).map((f) => f.year));
  return [...(brreg || []), ...(read || []).filter((f) => !have.has(f.year))].sort((a, b) => b.year - a.year);
}

/** Read status rows (GET …/accounts) by year. */
const byYear = (accRows) => new Map((accRows || []).map((r) => [r.year, r]));
export const busyRow = (r) => !!r && (r.status === "queued" || r.status === "running");

/**
 * The PDF years a click on "Les inn" should offer: not shown as data, not read, not being read, not failed. Newest
 * first. pdfYears = years Brreg has as PDF.
 */
export function unreadYears(pdfYears, dataYears, accRows) {
  const rows = byYear(accRows), data = new Set(dataYears);
  return (pdfYears || []).filter((y) => !data.has(y) && !rows.has(y)).sort((a, b) => b - a);
}

/** Brreg data years right after the years asked for, whose PDF isn't read: their prior-year column checks the year
 * before, so they are read along with it. A Brreg year itself is checked against Brreg's data and needs none. */
export function controlYears(years, brregYears, pdfYears, accRows) {
  const rows = byYear(accRows), b = new Set(brregYears), p = new Set(pdfYears);
  return [...new Set(years.filter((y) => !b.has(y)).map((y) => y + 1))]
    .filter((y) => b.has(y) && p.has(y) && !years.includes(y) && (!rows.has(y) || rows.get(y).status === "failed"));
}

/** Why a read failed, shown next to a "Prøv igjen" button: the server's message without its own "Prøv igjen". */
export function failReason(error) {
  const why = (error || "").replace(/\s*Prøv igjen( senere)?\.?\s*$/, "").trim();
  return why === "Avbrutt av omstart." ? "ble avbrutt da appen ble oppdatert." : why || "feilet.";
}

/** A PDF year's main row the read didn't find (f.missing), shown as "ikke funnet" rather than "–". */
export const notFound = (f, field) => !!f.pdf && !!field && (f.missing || []).includes(field);

/** The note above the table for flagged figures ({st} each), or null. A figure that differs in the next
 * year's accounts is usually restated (omarbeidet), so it gets a quiet note; only Brreg ≠ PDF is red. */
export function flagNote(flags) {
  const n = (st) => flags.filter((f) => f.st === st).length;
  const check = n("check"), restated = n("restated");
  if (check && restated) return { cls: "red", text: `${check} tall å sjekke, ${restated} endret i neste års regnskap. Vis` };
  if (check) return { cls: "red", text: `${check} tall å sjekke. Vis` };
  if (restated) return { cls: "soft", text: `${restated} tall er endret i neste års regnskap. Vis` };
  return null;
}

/** One line under a year's header: {cls: "" | "ok" | "amber" | "red", text}. checks = {field: check} or undefined. */
export function yearStatus(f, checks, pdfYears, accRows) {
  const rows = byYear(accRows);
  if (!f.pdf) {
    if (!checks) return { cls: "", text: "Brreg" };
    return Object.values(checks).some((c) => c.st === "check") ? { cls: "red", text: "Brreg ≠ PDF" } : { cls: "ok", text: "Brreg ✓ PDF" };
  }
  const cs = Object.values(f.checks || {});
  const n = (st) => cs.filter((c) => c.st === st).length;
  if (n("check")) return { cls: "red", text: `${n("check")} å sjekke` };
  if (n("restated")) return { cls: "amber", text: `${n("restated")} omarbeidet` };
  if ((f.missing || []).length) return { cls: "amber", text: `${f.missing.length} ikke funnet` };
  if (n("ok")) return { cls: "ok", text: "✓ kontrollert" };
  const next = f.year + 1;
  if (busyRow(rows.get(next))) return { cls: "", text: `venter på ${next}` };
  return { cls: "", text: "1 kilde" };
}

/** Progress of a read for a column header. */
export function progressText(r) {
  if (r.status === "queued") return "i kø";
  if (r.phase === "leser" && r.pages_total) return `s. ${Math.min(r.pages_done + 1, r.pages_total)}/${r.pages_total}`;
  if (r.phase === "kontrollerer") return "kontrollerer";
  return "henter PDF";
}

/** Where a figure came from, in words: "2021-regnskapet, fjorårstall, s. 4". */
export function sourceLabel(s) {
  if (s.col === "brreg") return "Brreg (data)";
  const where = s.col === "prior" ? ", fjorårstall" : s.col === "note" ? `, note ${s.note || "?"}` : "";
  return `${s.year}-regnskapet${where}${s.page ? `, s. ${s.page}` : ", regnet ut"}`;
}

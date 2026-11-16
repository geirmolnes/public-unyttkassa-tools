// Key figures across years (src/static/figures.js). Run with `bun test`.
import { describe, expect, test } from "bun:test";
import { BALANCE_ROWS, RESULT_ROWS, exportFilename, exportSheets, flagNote, spanLabel, taxOf, toTsv, visibleRows } from "../../src/static/figures.js";

// Regression: three restated figures read «3 tall stemmer ikke mellom to regnskap» in red, as if the read were wrong.
describe("note on flagged figures", () => {
  const fl = (st, k) => Array.from({ length: k }, () => ({ st }));
  test.each([
    ["none", [], null],
    ["restated only: quiet, says what changed", fl("restated", 3), { cls: "soft", text: "3 tall er endret i neste års regnskap. Vis" }],
    ["one restated", fl("restated", 1), { cls: "soft", text: "1 tall er endret i neste års regnskap. Vis" }],
    ["Brreg ≠ PDF only: red", fl("check", 2), { cls: "red", text: "2 tall å sjekke. Vis" }],
    ["both: red, check first", [...fl("restated", 3), ...fl("check", 1)], { cls: "red", text: "1 tall å sjekke, 3 endret i neste års regnskap. Vis" }],
  ])("%s", (_, flags, want) => expect(flagNote(flags)).toEqual(want));
});

describe("span label on the all-years button", () => {
  const h = (...years) => years.map((year) => ({ year }));
  test.each([
    ["three years", h(2025, 2024, 2023), "2023–25"],
    ["a fourth year moves the end", h(2026, 2025, 2024, 2023), "2023–26"],
    ["two years", h(2025, 2024), "2024–25"],
    ["one year", h(2025), "2025"],
    ["across a century", h(2101, 2100, 2099), "2099–2101"],
  ])("%s", (_, history, expected) => {
    expect(spanLabel(history)).toBe(expected);
  });
});

describe("rows", () => {
  const labels = (rows) => rows.map(([label]) => label);

  test("sub rows hide when no year has them", () => {
    expect(labels(visibleRows(BALANCE_ROWS, [{ bankinnskudd: null }, {}]))).not.toContain("herav bankinnskudd");
  });

  test.each([
    ["goodwill", BALANCE_ROWS, { goodwill: 1 }, "herav goodwill"],
    ["interest to group companies", RESULT_ROWS, { rentekostnad_konsern: 0 }, "herav rente til selskap i konsernet"],
    ["netto finans", RESULT_ROWS, { netto_finans: -5 }, "Netto finans"],
    ["extraordinary items", RESULT_ROWS, { ekstraordinaere_poster: 2 }, "Ekstraordinære poster"],
    ["totalresultat", RESULT_ROWS, { totalresultat: 3 }, "Totalresultat"],
  ])("optional line shows when a year has it: %s", (_, rows, year, label) => {
    expect(labels(visibleRows(rows, [{}, year]))).toContain(label);
  });

  test("optional lines stay hidden when no year has them", () => {
    expect(labels(visibleRows(RESULT_ROWS, [{ totalresultat: null }, {}]))).not.toContain("Totalresultat");
  });

  test("sub rows show when one year of several has them", () => {
    expect(labels(visibleRows(BALANCE_ROWS, [{}, { bankinnskudd: 0 }]))).toContain("herav bankinnskudd");
  });

  test.each([
    ["real tax wins", { skattekostnad: 7447000, skattekostnad_avledet: 7400000 }, 7447000],
    ["derived when Brreg has no tax line", { skattekostnad: null, skattekostnad_avledet: 7447000 }, 7447000],
    ["tax income stays negative", { skattekostnad: -29999000 }, -29999000],
    ["neither", {}, undefined],
  ])("tax: %s", (_, f, expected) => {
    expect(taxOf(f)).toBe(expected);
  });

  test("Sum EK og gjeld adds equity and debt, falls back to total assets", () => {
    const get = BALANCE_ROWS.find(([l]) => l === "Sum EK og gjeld")[1];
    expect([get({ egenkapital: 30, sum_gjeld: 84 }), get({ egenkapital: null, sum_gjeld: 84, sum_eiendeler: 114 })]).toEqual([114, 114]);
  });

  test("result rows keep the statement order", () => {
    expect(labels(visibleRows(RESULT_ROWS, [{}]))).toEqual([
      "Driftsinntekter", "Driftskostnader", "Driftsresultat", "Finansinntekter", "Finanskostnader",
      "Resultat før skatt", "Skattekostnad", "Årsresultat",
    ]);
  });
});

describe("export sheets", () => {
  // Newest first, as the API sends it. 2024 lacks salary; only 2025 has goodwill.
  const history = [
    { year: 2025, currency: "NOK", driftsinntekter: 257000, driftskostnader: 1311000, loennskostnad: 57000,
      skattekostnad: 7447000, goodwill: 1500.4, egenkapital: 29969000, innskutt_egenkapital: 1270000, ek_andel: 0.26268 },
    { year: 2024, currency: "NOK", driftsinntekter: 233268, driftskostnader: 973000, skattekostnad: -29999000,
      egenkapital: 50205342, ek_andel: 0.4240 },
  ];
  const [res, bal] = exportSheets(history);
  const row = (sheet, label) => sheet.rows.find((r) => r[0] === label);

  test("two sheets, header names the currency and lists years oldest first", () => {
    expect([res.name, bal.name, res.rows[0], bal.rows[0]]).toEqual([
      "Resultat", "Balanse", ["Post (NOK)", 2024, 2025], ["Post (NOK)", 2024, 2025],
    ]);
  });

  test.each([
    ["income as is, whole kroner", "Driftsinntekter", ["Driftsinntekter", 233268, 257000]],
    ["costs negative, as on screen", "Driftskostnader", ["Driftskostnader", -973000, -1311000]],
    ["a missing year is null", "herav lønn", ["herav lønn", null, -57000]],
    ["tax income comes out positive", "Skattekostnad", ["Skattekostnad", 29999000, -7447000]],
  ])("resultat: %s", (_, label, expected) => {
    expect(row(res, label)).toEqual(expected);
  });

  test.each([
    ["rounded to whole units", "herav goodwill", ["herav goodwill", null, 1500]],
    ["paid-in equity follows equity", "herav innskutt egenkapital", ["herav innskutt egenkapital", null, 1270000]],
    ["EK-andel in percent, one decimal", "EK-andel (%)", ["EK-andel (%)", 42.4, 26.3]],
  ])("balanse: %s", (_, label, expected) => {
    expect(row(bal, label)).toEqual(expected);
  });

  test("lines no year has are left out, the core lines are always there", () => {
    expect([row(res, "Totalresultat"), row(bal, "herav opptjent egenkapital"), row(res, "Årsresultat")]).toEqual([
      undefined, undefined, ["Årsresultat", null, null],
    ]);
  });

  test("bold marks sum and total rows, indent marks herav rows (header is row 0)", () => {
    const at = (sheet, idx) => idx.map((i) => sheet.rows[i][0]);
    expect([at(res, res.bold), at(res, res.indent)]).toEqual([
      ["Driftsresultat", "Resultat før skatt", "Årsresultat"],
      ["herav lønn"],
    ]);
  });

  test("a USD company says so in the header", () => {
    expect(exportSheets([{ year: 2025, currency: "USD" }])[0].rows[0]).toEqual(["Post (USD)", 2025]);
  });
});

describe("clipboard text", () => {
  const sheets = [
    { name: "Resultat", rows: [["Post (NOK)", 2024, 2025], ["Årsresultat", -23177000, null]] },
    { name: "Balanse", rows: [["Post (NOK)", 2025], ["EK-andel (%)", 26.3], ["navn\tmed tab", 1]] },
  ];
  test("tabs between cells, sheet name above its rows, blank line between sheets", () => {
    expect(toTsv(sheets)).toBe(
      "Resultat\nPost (NOK)\t2024\t2025\nÅrsresultat\t-23177000\t\n\n" +
      "Balanse\nPost (NOK)\t2025\nEK-andel (%)\t26,3\nnavn med tab\t1\n",
    );
  });
});

describe("export filename", () => {
  const h = (...years) => years.map((year) => ({ year }));
  test.each([
    ["span", "HJR HOLDING AS", "selskap", h(2025, 2024, 2023), "HJR_HOLDING_AS_selskapsregnskap_2023-2025.xlsx"],
    // Regression: built as `${scope}sregnskap`, which gave "konsernsregnskap".
    ["group accounts", "HJR HOLDING AS", "konsern", h(2025), "HJR_HOLDING_AS_konsernregnskap_2025.xlsx"],
    ["keeps æøå, drops punctuation", "Bølgen & Moi A/S", "selskap", h(2025), "Bølgen_Moi_A_S_selskapsregnskap_2025.xlsx"],
    ["empty name", "", "selskap", h(2025), "selskap_selskapsregnskap_2025.xlsx"],
  ])("%s", (_, name, scope, history, expected) => {
    expect(exportFilename(name, scope, history)).toBe(expected);
  });
});

describe("years read from PDFs", () => {
  const { mergeHistory, notFound, unreadYears, controlYears, failReason, yearStatus, progressText, sourceLabel } = require("../../src/static/figures.js");
  const ok = (year) => ({ year, status: "ok" });

  test("merge keeps Brreg's year when both have it, newest first", () => {
    expect(mergeHistory([{ year: 2024, b: 1 }, { year: 2023, b: 1 }], [{ year: 2023, p: 1 }, { year: 2022, p: 1 }]))
      .toEqual([{ year: 2024, b: 1 }, { year: 2023, b: 1 }, { year: 2022, p: 1 }]);
  });

  test.each([
    ["nothing read", [], [2022, 2021, 2020]],
    ["read, running and failed years are not offered", [ok(2022), { year: 2021, status: "running" }, { year: 2020, status: "failed" }], []],
  ])("unread: %s", (_, rows, expected) => {
    expect(unreadYears([2020, 2021, 2022, 2023, 2024], [2023, 2024], rows)).toEqual(expected);
  });

  test.each([
    ["2022 brings 2023 along", [2022], [], [2023]],
    ["2023 already read", [2022], [ok(2023)], []],
    ["2023 failed earlier is tried again", [2022], [{ year: 2023, status: "failed" }], [2023]],
    ["older years need no Brreg year", [2020], [], []],
    ["no PDF for 2023", [2022], [], [], [2020, 2021, 2022]],
    ["a Brreg year brings nothing along", [2024], [], [], [2022, 2023, 2024, 2025]],
  ])("control: %s", (_, years, rows, expected, pdf = [2020, 2021, 2022, 2023]) => {
    expect(controlYears(years, [2023, 2024, 2025], pdf, rows)).toEqual(expected);
  });

  test.each([
    ["restart", "Avbrutt av omstart. Prøv igjen.", "ble avbrutt da appen ble oppdatert."],
    ["generic", "Kunne ikke lese årsregnskapet. Prøv igjen senere.", "Kunne ikke lese årsregnskapet."],
    ["no retry hint", "Ikke lest: 1200 sider er mer enn grensen på 1000 sider per innlesing.", "Ikke lest: 1200 sider er mer enn grensen på 1000 sider per innlesing."],
    ["empty", null, "feilet."],
  ])("fail reason: %s", (_, error, expected) => {
    expect(failReason(error)).toBe(expected);
  });

  const pdfYear = (year, sts, col = "current") => ({ year, pdf: true, checks: Object.fromEntries(sts.map((st, i) => [`f${i}`, { st, src: [{ col }] }])) });
  test.each([
    ["one to check wins", pdfYear(2020, ["ok", "restated", "check"]), [], { cls: "red", text: "1 å sjekke" }],
    ["restated", pdfYear(2020, ["ok", "restated", "restated"]), [], { cls: "amber", text: "2 omarbeidet" }],
    ["both filings agree", pdfYear(2020, ["ok", "one"]), [], { cls: "ok", text: "✓ kontrollert" }],
    ["agree but rows not found", { ...pdfYear(2020, ["ok"]), missing: ["driftsinntekter", "driftskostnader"] }, [], { cls: "amber", text: "2 ikke funnet" }],
    ["to check outranks not found", { ...pdfYear(2020, ["check"]), missing: ["driftsinntekter"] }, [], { cls: "red", text: "1 å sjekke" }],
    ["next filing being read", pdfYear(2020, ["one"]), [{ year: 2021, status: "running" }], { cls: "", text: "venter på 2021" }],
    // Nothing reads 2021 unless asked, so there is nothing to wait for
    ["next filing exists but unread", pdfYear(2020, ["one"]), [], { cls: "", text: "1 kilde" }],
    ["next filing read, figure only in one", pdfYear(2020, ["one"]), [ok(2021)], { cls: "", text: "1 kilde" }],
    ["year before the oldest filing", pdfYear(2020, ["one"], "prior"), [], { cls: "", text: "1 kilde" }],
  ])("status: %s", (_, f, rows, expected) => {
    expect(yearStatus(f, undefined, [2020, 2021], rows)).toEqual(expected);
  });

  test.each([
    ["missing in a PDF year", { pdf: true, missing: ["driftsinntekter"] }, "driftsinntekter", true],
    ["found in a PDF year", { pdf: true, missing: ["driftsinntekter"] }, "driftskostnader", false],
    ["Brreg year keeps its dash", { missing: ["driftsinntekter"] }, "driftsinntekter", false],
    ["computed row has no field", { pdf: true, missing: [] }, undefined, false],
  ])("not found: %s", (_, f, field, expected) => {
    expect(notFound(f, field)).toBe(expected);
  });

  test.each([
    ["Brreg only", undefined, { cls: "", text: "Brreg" }],
    ["PDF agrees", { a: { st: "ok" } }, { cls: "ok", text: "Brreg ✓ PDF" }],
    ["PDF differs", { a: { st: "ok" }, b: { st: "check" } }, { cls: "red", text: "Brreg ≠ PDF" }],
  ])("Brreg year: %s", (_, checks, expected) => {
    expect(yearStatus({ year: 2023 }, checks, [], [])).toEqual(expected);
  });

  test.each([
    [{ status: "queued" }, "i kø"],
    [{ status: "running", phase: "henter" }, "henter PDF"],
    [{ status: "running", phase: "leser", pages_done: 0, pages_total: 31 }, "s. 1/31"],
    [{ status: "running", phase: "leser", pages_done: 31, pages_total: 31 }, "s. 31/31"],
    [{ status: "running", phase: "kontrollerer" }, "kontrollerer"],
  ])("progress %#", (r, expected) => {
    expect(progressText(r)).toBe(expected);
  });

  test.each([
    [{ col: "current", year: 2021, page: 3 }, "2021-regnskapet, s. 3"],
    [{ col: "prior", year: 2022, page: 4 }, "2022-regnskapet, fjorårstall, s. 4"],
    [{ col: "note", year: 2021, page: 12, note: "7" }, "2021-regnskapet, note 7, s. 12"],
    [{ col: "current", year: 2021, page: null }, "2021-regnskapet, regnet ut"],
    [{ col: "brreg", year: 2023 }, "Brreg (data)"],
  ])("source %#", (s, expected) => {
    expect(sourceLabel(s)).toBe(expected);
  });
});

// Regnskapsleser HTML helpers (src/static/reader.js) on a small payload shaped like the API's. Run with `bun test`.
import { describe, expect, test } from "bun:test";
import { checkStep, flagFor, fmtKr, mainHtml, markSnippet, noteSection, pageListHtml, pageSpan, pageTitle, sectionForPage, sections } from "../../src/static/reader.js";

const flag = (page, values, note = null, extra = {}) => ({ page, kind: "k", text: "t", short: "s", values, row: null, note, ...extra });
const DATA = {
  statements: { resultat: { title: "Resultatregnskap", pages: [20], lines: [] } },
  notes: [
    { number: "8", title: "Egenkapital", pages: [31], seq: [] },
    { number: "9", title: "Bankinnskudd", pages: [31], seq: [] },
    { number: "4", title: "Pensjon", pages: [27, 28], seq: [] },
  ],
  misc: [{ page: 6, kind: "beretning", seq: [] }, { page: 7, kind: "beretning", seq: [] }, { page: 33, kind: "annet", seq: [] }],
  checks: [flag(31, [18000000], 1), flag(28, [-744099], 2), flag(31, [574597], null)],
};

describe("pageSpan", () => {
  test.each([
    [[], "–"], [[4], "s. 4"], [[27, 28], "s. 27–28"], [[6, 7, 8], "s. 6–8"], [[1, 8, 9], "3 sider"], [[1, 3], "2 sider"],
  ])("%p → %p", (pages, want) => expect(pageSpan(pages)).toBe(want));
});

describe("fmtKr", () => {
  test.each([[null, "–"], [undefined, "–"], [0, "0"], [-744099, "−744 099"], [999, "999"], [1000, "1 000"]])(
    "%p → %p", (v, want) => expect(fmtKr(v)).toBe(want));
});

describe("sections", () => {
  const secs = sections(DATA);
  const count = (id) => secs.find((s) => s.id === id).checks.length;
  test("a flag tied to a note counts only on that note", () => expect([count("n0"), count("n1")]).toEqual([1, 2]));
  test("a flag on a note's second page counts on that note", () => expect(count("n2")).toBe(1));
  test("parts in reading order, empty parts left out", () =>
    expect(secs.map((s) => s.id)).toEqual(["resultat", "n0", "n1", "n2", "beretning", "annet"]));
});

describe("flagFor", () => {
  const checks = [flag(5, [740]), flag(5, [], null, { row: "Sum" }), flag(6, [9], null, { short: null })];
  test.each([
    ["value on its page", 5, [740, 1], "x", 0],
    ["same value, other page", 6, [740], "x", null],
    ["row label", 5, [1], "Sum", 1],
    ["null values never match", 5, [null], "x", null],
    ["flags without a short line are list-only", 6, [9], "x", null],
  ])("%s", (_, page, values, label, want) => expect(flagFor(checks, page, values, label)).toBe(want === null ? null : checks[want]));
});

describe("noteSection", () => {
  test.each([["9", "n1"], ["4, 6", "n2"], ["12", null]])("note %p → %p", (ref, want) => expect(noteSection(DATA, ref)).toBe(want));
});

describe("konsern", () => {
  const d = {
    ...DATA,
    konsern: { resultat: { title: "Resultatregnskap", pages: [40], lines: [] } },
    notes: [...DATA.notes, { number: "2", title: "Konsernets inntekter", pages: [42], seq: [], for: "konsern" }],
  };
  const secs = sections(d);
  test("group statements and notes sit under Konsern", () =>
    expect(secs.filter((s) => s.group === "Konsern").map((s) => s.id)).toEqual(["k-resultat", "n3"]));
  test("group notes are not listed under the company's notes", () =>
    expect(secs.filter((s) => s.group === "Noter").map((s) => s.id)).toEqual(["n0", "n1", "n2"]));
});

describe("markSnippet", () => {
  test("marks hits and escapes the text", () =>
    expect(markSnippet("a <b> \x01garanti\x02 & c")).toBe("a &lt;b&gt; <mark>garanti</mark> &amp; c"));
});

describe("sectionForPage", () => {
  const secs = sections(DATA);
  test.each([
    ["the hit's note on a shared page", 31, "9", "n1"],
    ["first note when the hit has none", 31, null, "n0"],
    ["a note's second page", 28, null, "n2"],
    ["a statement page", 20, null, "resultat"],
    ["a page nothing shows", 99, null, null],
  ])("%s", (_, page, note, want) => expect((sectionForPage(secs, DATA, page, note) || { id: null }).id).toBe(want));
});


describe("scan header: page titles, checks to step through, the page list", () => {
  const page = (n, kind, extra = {}) => ({ n, kind, printout: false, for: "selskap", ...extra });
  const d = {
    ...DATA,
    konsern: { resultat: { title: "Resultatregnskap", pages: [40], lines: [] } },
    notes: [...DATA.notes, { number: "2", title: "Konsernets inntekter", pages: [42], seq: [], for: "konsern" }],
    pages: [page(2, "resultat", { printout: true }), page(5, "balanse", { printout: true, for: "konsern" }), page(6, "annet"),
      page(20, "resultat"), page(28, "noter"), page(31, "noter"), page(33, "annet"), page(40, "resultat", { for: "konsern" }),
      page(42, "noter", { for: "konsern" })],
  };
  const secs = sections(d);
  test.each([
    ["a statement", 20, "Resultatregnskap"],
    ["one note", 28, "Note 4 Pensjon"],
    ["two notes on a page", 31, "Noter 8, 9"],
    ["the group's statement", 40, "Konsern · Resultatregnskap"],
    ["the group's note", 42, "Konsern · Note 2 Konsernets inntekter"],
    ["a misc page", 6, "Årsberetning"],
    ["Brreg's printout", 2, "Brregs utskrift · Resultatregnskap"],
    ["Brreg's printout of the group", 5, "Brregs utskrift · Konsern · Balanse"],
  ])("title: %s", (_, n, want) => expect(pageTitle(d, secs, n)).toBe(want));

  test.each([
    ["before the first", 5, { prev: null, next: 28, at: 0, total: 2 }],
    ["on the first", 28, { prev: null, next: 31, at: 1, total: 2 }],
    ["between", 30, { prev: 28, next: 31, at: 0, total: 2 }],
    ["on the last", 31, { prev: 28, next: null, at: 2, total: 2 }],
  ])("checks: %s", (_, n, want) => expect(checkStep(d, n)).toEqual(want));
  test("no checks, nothing to step", () => expect(checkStep({ checks: [] }, 3)).toEqual({ prev: null, next: null, at: 0, total: 0 }));

  const listed = (html) => [...html.matchAll(/data-page="(\d+)"/g)].map((m) => Number(m[1]));
  test.each([
    ["no filter: every page", "", [2, 5, 6, 20, 28, 31, 33, 40, 42]],
    ["page number", "31", [31]],
    ["words in the title, any case", "KONSERN", [5, 40, 42]],
    ["a note's name on a page of several notes", "bankinnskudd", [31]],
  ])("list: %s", (_, q, want) => expect(listed(pageListHtml(d, secs, 20, q))).toEqual(want));
  test("list marks the current page and counts its checks", () => {
    const html = pageListHtml(d, secs, 31, "31");
    expect([html.includes('aria-current="true"'), html.includes("2 å sjekke")]).toEqual([true, true]);
  });
  test("list says when nothing matches", () => expect(pageListHtml(d, secs, 20, " xyz ")).toContain("Ingen sider passer «xyz»"));
});

describe("last year's column against last year's filing", () => {
  const info = [{ page: 3, kind: "Ulikt fjorårets regnskap", text: "«Sum eiendeler» for 2024 står som 1 her, men 2 i regnskapet for 2024.", row: "Sum eiendeler", note: null, values: [] }];
  const d = {
    checks: [], info,
    statements: { balanse: { title: "Balanse", copy: "brreg_printout", pages: [3], unit_scale: 1, current_year: 2025, prior_year: 2024,
      lines: [{ label: "Sum eiendeler", key: "sum_eiendeler", cur: 5, prior: 1, page: 3, ref: null, also: null }] } },
  };

  test("is information under the checks, and doesn't count as one", () => {
    const h = mainHtml(d, [], "checks", null);
    expect(h).toContain("0 å sjekke");
    expect(h).toContain("Til informasjon (1)");
    expect(h).toContain('class="chk-list info"');
  });

  test("marks the line in the statement, not as a flag", () => {
    const h = mainHtml(d, [], "balanse", null);
    expect(h).toContain('<span class="inf"');
    expect(h).not.toContain("flagged");
  });
});

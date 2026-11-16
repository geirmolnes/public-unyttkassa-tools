// Kunngjøringer on the company page (src/static/kunngjoringer.js), on notices shaped like the API's
// (parsed from w2.brreg.no; see tests/test_kunngjoringer.py). Run with `bun test`.
import { describe, expect, test } from "bun:test";
import { bankruptcy, bankruptcyFacts, bankruptcyHead, noticeHtml, search, shortTitle, summary, tracks, valHtml } from "../../src/static/kunngjoringer.js";

const n = (date, title, parts, extra = {}) => ({ kid: date.replaceAll("-", ""), date, type: title, title, read: true, parts, url: "https://w2.brreg.no/x", ...extra });
const board = (date, leader, members) => n(date, "Endring av styre", [["f", "Styre", ""], ["f", "Styrets leder", `${leader}, Gate 1, 7000 TRONDHEIM`], ["f", "Styremedlem", members.join("\n")]]);
const auditor = (date, name) => n(date, "Endring av revisor", [["f", "Revisor", `Org.nr 935 416 779 ${name}, Ruseløkkveien 30, 0251 OSLO`]]);
const unread = (date, type) => ({ kid: `u${date}`, date, type, read: false });
const ITEMS = [
  auditor("2026-10-05", "CEDRA NORGE AS"),
  n("2026-09-12", "Endring av daglig leder", [["f", "Daglig leder", "Utgår"]]),
  n("2026-07-27", "Godkjent årsregnskap", [["p", "Regnskapsregisteret har den 26.06.2026 mottatt komplett årsregnskap for regnskapsåret 2025 fra:"]]),
  board("2024-07-05", "Lars Holm", ["Peder Ås", "Marte Kirkerud", "Representant for A-aksjonærene"]),
  board("2023-09-15", "Lars Holm", ["Peder Ås"]),
  auditor("2023-12-04", "ERNST & YOUNG AS"),
  n("2023-06-30", "Endring av vedtektsdato", [["f", "Vedtektsdato", "30.06.2023"]]),
  n("2015-02-03", "Endring av foretaksnavn", [["f", "Nytt foretaksnavn", "LIQUID BARCODES AS"]]),
  n("2014-06-26", "Nyregistrering", [["f", "Kapital", "NOK 100.800,00"], ["f", "Daglig leder", "Ole Vold"],
    ["f", "Styrets leder", "Tor Østby, Storgata 2, 1362 HOSLE"], ["f", "Revisjon av årsregnskap", "Foretakets årsregnskap skal ikke revideres."]],
    { name: "TEKÅGEL INVEST 771 AS", address: "Haakon VII's gate 10, 0161 OSLO" }),
  unread("2013-05-01", "Revisor"),
  unread("2012-05-01", "Kapital"),
];

describe("one-line summary", () => {
  test.each([
    ["auditor's name without org.nr and address", ITEMS[0], { v: "CEDRA NORGE AS" }],
    ["a removed role is an event, not a name", ITEMS[1], { v: "Ingen daglig leder", ev: "utgått" }],
    ["accounts year and when received", ITEMS[2], { v: "For 2025, mottatt 26.06.2026" }],
    ["board: leader, members without the representative note", ITEMS[3], { v: "Leder Lars Holm · 2 medlemmer", ev: undefined }],
    ["one member", ITEMS[4], { v: "Leder Lars Holm · 1 medlem", ev: undefined }],
    ["new name", ITEMS[7], { v: "LIQUID BARCODES AS" }],
    ["registration: capital and daily manager", ITEMS[8], { v: "NOK 100.800,00 · daglig leder Ole Vold" }],
    ["unread says nothing yet", ITEMS[9], null],
  ])("%s", (_, it, expected) => {
    expect(summary(it)).toEqual(expected);
  });

  test("a merger names the companies, the one merged away first", () => {
    const m = n("2025-11-25", "Gjennomføring av fusjon", [
      ["h", "Overtakende foretak"], ["f", "Foretaksnavn", "LÆRINGSVERKSTEDET AS"], ["f", "Organisasjonsnummer", "878 620 852"],
      ["h", "Overdragende foretak"], ["f", "Foretaksnavn", "SOLKOLLEN BARNEHAGE AS"],
    ]);
    expect(summary(m)).toEqual({ v: "fra SOLKOLLEN BARNEHAGE AS · til LÆRINGSVERKSTEDET AS" });
  });

  test("an event shows as text plus a label; plain values are escaped", () => {
    expect([valHtml({ v: "Ingen daglig leder", ev: "utgått" }), valHtml({ v: "A & B" }), valHtml(null)])
      .toEqual(['Ingen daglig leder <span class="kn-tag">utgått</span>', "A &amp; B", ""]);
  });

  test("title drops «Endring av»; unread uses Brreg's type", () => {
    expect([shortTitle(ITEMS[0]), shortTitle(ITEMS[9]), shortTitle(ITEMS[8])]).toEqual(["Revisor", "Revisor", "Nyregistrering"]);
  });
});

describe("per tema", () => {
  const T = Object.fromEntries(tracks(ITEMS).map((t) => [t.key, t]));
  const vals = (t) => t.xs.map((x) => [x.v, x.ev, x.it.date]);

  test("each topic newest first, back to the registration", () => {
    expect(vals(T.revisor)).toEqual([
      ["CEDRA NORGE AS", undefined, "2026-10-05"],
      ["ERNST & YOUNG AS", undefined, "2023-12-04"],
      ["Ingen revisor", "fravalgt", "2014-06-26"],
    ]);
    expect(vals(T.navn)).toEqual([["LIQUID BARCODES AS", undefined, "2015-02-03"], ["TEKÅGEL INVEST 771 AS", undefined, "2014-06-26"]]);
  });

  test("the current daily manager is «none» after the role was removed", () => {
    expect(vals(T.dl)[0]).toEqual(["Ingen daglig leder", "utgått", "2026-09-12"]);
  });

  test("a board change keeping its leader stays with the notice that made them leader", () => {
    expect(vals(T.styre)).toEqual([["Lars Holm", undefined, "2023-09-15"], ["Tor Østby", undefined, "2014-06-26"]]);
  });

  test("unread notices count toward their topic; a topic with only unread ones still shows", () => {
    expect([T.revisor.all.length, T.revisor.unread, T.kapital.unread, T.kapital.xs.length]).toEqual([4, 1, 1, 1]);
  });

  test("notices fitting no topic go under «Annet», last", () => {
    const keys = tracks(ITEMS).map((t) => t.key);
    expect([keys.at(-1), T.annet.other, T.annet.all.map((it) => it.title)]).toEqual(["annet", true, ["Endring av vedtektsdato"]]);
  });

  test("no notices, no topics", () => {
    expect(tracks([])).toEqual([]);
  });
});

describe("search", () => {
  test.each([
    ["a word in the text", "ernst", ["2023-12-04"]],
    ["every word must match", "lars peder marte", ["2024-07-05"]],
    ["a year finds that year's notices", "2014", ["2014-06-26"]],
    ["unread notices are found by type", "revisor", ["2026-10-05", "2023-12-04", "2013-05-01"]],
    ["case and spaces don't matter", "  CEDRA  ", ["2026-10-05"]],
    ["empty finds nothing", " ", []],
  ])("%s", (_, q, dates) => {
    expect(search(ITEMS, q).map((it) => it.date)).toEqual(dates);
  });
});

describe("bankruptcy", () => {
  const k = n("2026-09-18", "Konkurs - åpning", [
    ["h", "Ved HORDALAND TINGRETT er det åpnet konkurs i boet til:"],
    ["f", "Konkurs åpnet", "18.09.2026"], ["f", "Åpnet etter", "Oppbud"],
    ["p", "Krav i boet meldes bostyrer, Adv. Hans Tastad, Storgata 4, 5003 BERGEN innen 23.10.2026. Fristdagen er 18.09.2026."],
    ["p", "Første skiftesamling blir holdt 12.11.2026 kl. 10:45 i Hordaland tingrett."],
  ]);

  test("is picked out, headed in plain words, with its facts", () => {
    expect([bankruptcy([k, ...ITEMS]), bankruptcy(ITEMS), bankruptcyHead(k)]).toEqual([k, undefined, "Konkurs åpnet 18.09.2026"]);
    expect(bankruptcyFacts(k)).toEqual([
      ["Åpnet etter", "Oppbud"], ["Domstol", "Hordaland Tingrett"], ["Bostyrer", "Adv. Hans Tastad"],
      ["Krav innen", "23.10.2026"], ["Skiftesamling", "12.11.2026"],
    ]);
  });

  test("an unread one is known by type, without facts", () => {
    expect([bankruptcy([unread("2026-09-18", "Konkursåpning")]).type, bankruptcyFacts(unread("2026-09-18", "Konkursåpning"))]).toEqual(["Konkursåpning", []]);
  });
});

test("a notice in full escapes text, groups label rows and marks a removed role", () => {
  const html = noticeHtml(n("2024-07-05", "Endring av styre", [["h", "Styre <b>"], ["f", "Styre", ""], ["f", "Styrets leder", "A & B"], ["f", "Daglig leder", "Utgår"]], { register: "Foretaksregisteret" }));
  expect(html).toContain('<p class="kn-h">Styre &lt;b&gt;</p><dl class="kn-f"><dt class="grp">Styre</dt><dt>Styrets leder</dt><dd>A &amp; B</dd>');
  expect(html).toContain('<dd><span class="kn-tag">Utgår</span> rollen er fjernet</dd>');
  expect(html).toContain("Foretaksregisteret 05.07.2024");
});

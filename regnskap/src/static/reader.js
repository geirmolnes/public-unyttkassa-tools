// Regnskapsleser: HTML for one read årsregnskap (GET /api/company/{orgnr}/accounts/{year}). Pure functions, no
// DOM, so bun test can check them. app.js wires the clicks: contents list | extracted section | scanned page.
import { esc } from "./format.js";

export const KIND = { resultat: "Resultatregnskap", balanse: "Balanse", kontantstrom: "Kontantstrøm", noter: "Noter",
  beretning: "Årsberetning", revisjon: "Revisjonsberetning", annet: "Annet" };
const STMT = ["resultat", "balanse", "kontantstrom"];

/** Whole kroner with a real minus and thin-space groups, "–" for none. */
export function fmtKr(v) {
  if (v === null || v === undefined) return "–";
  return (v < 0 ? "−" : "") + Math.abs(v).toLocaleString("nb-NO").replace(/\s/g, " ");
}

/** "s. 4", "s. 27–28", or "7 sider" when the pages aren't consecutive. */
export function pageSpan(ps) {
  if (!ps.length) return "–";
  if (ps.length === 1) return `s. ${ps[0]}`;
  return ps[ps.length - 1] - ps[0] === ps.length - 1 ? `s. ${ps[0]}–${ps[ps.length - 1]}` : `${ps.length} sider`;
}

export const nCheck = (n) => `${n} å sjekke`;
const noteLabel = (n) => (n.number ? `Note ${n.number}` : "Note");

/** Reading-order parts of the filing, each with its pages and the checks on those pages. */
export function sections(d) {
  const group = d.konsern || {};
  const out = STMT.filter((k) => d.statements[k]).map((k) => ({ id: k, title: d.statements[k].title, group: "Oppstillinger", pages: d.statements[k].pages }));
  d.notes.forEach((n, i) => { if (n.for !== "konsern") out.push({ id: `n${i}`, title: n.title, num: n.number, group: "Noter", pages: n.pages }); });
  // The group's own statements and notes, when the filing has konsern accounts.
  STMT.filter((k) => group[k]).forEach((k) => out.push({ id: `k-${k}`, title: group[k].title, group: "Konsern", pages: group[k].pages }));
  d.notes.forEach((n, i) => { if (n.for === "konsern") out.push({ id: `n${i}`, title: n.title, num: n.number, group: "Konsern", pages: n.pages }); });
  for (const [id, kinds, title] of [["beretning", ["beretning"], "Årsberetning"], ["revisjon", ["revisjon"], "Revisjonsberetning"], ["annet", ["annet"], "Andre sider"]]) {
    const pages = d.misc.filter((m) => kinds.includes(m.kind)).map((m) => m.page);
    if (pages.length) out.push({ id, title, group: "Annet", pages });
  }
  // A flag tied to one note counts only there; page-wide flags count on every part showing that page.
  out.forEach((s) => {
    const i = s.id[0] === "n" ? Number(s.id.slice(1)) : null;
    s.checks = d.checks.filter((c) => s.pages.includes(c.page) && (c.note === null || c.note === undefined || i === null || c.note === i));
  });
  return out;
}

/** The check that explains a table row: by value on that page, else by row label (sums that don't add up). */
export function flagFor(checks, page, values, label) {
  return checks.find((c) => c.page === page && c.short && (
    (c.values.length && values.some((v) => v !== null && c.values.includes(v))) || (c.row && c.row === label))) || null;
}

const why = (c) => (c ? `<span class="why">${esc(c.short)}</span>` : "");

export function tocHtml(d, secs, cur) {
  const groups = [...new Set(secs.map((s) => s.group))];
  const btn = (s) => `<li><button type="button" data-sec="${s.id}" aria-current="${cur === s.id}" title="${esc(s.title)}">
    <span class="tt">${s.num ? `<b class="tn">${esc(s.num)}</b> ` : ""}${esc(s.title)}</span>
    <span class="tc" title="${s.checks.length ? nCheck(s.checks.length) : ""}">${s.checks.length || ""}</span><span class="tp">${pageSpan(s.pages)}</span></button></li>`;
  return `<ul><li><button type="button" data-sec="checks" aria-current="${cur === "checks"}"><span class="tt"><b>Til kontroll</b> <span class="tp">(antall)</span></span><span class="tc">${d.checks.length}</span><span></span></button></li></ul>` +
    groups.map((g) => `<h2>${g}</h2><ul>${secs.filter((s) => s.group === g).map(btn).join("")}</ul>`).join("");
}

/** One note table; the label column is the first header when headers outnumber the values. */
export function noteTable(t, page, checks) {
  const nv = Math.max(0, ...t.rows.map((r) => r.values.length));
  const vcols = t.columns.slice(t.columns.length - nv);
  const lhead = t.columns.length > nv ? t.columns[0] : "";
  const head = `<tr><th>${esc(lhead)}</th>${vcols.map((c) => `<th class="n">${esc(c)}</th>`).join("")}</tr>`;
  const body = t.rows.map((r) => {
    const c = flagFor(checks, page, r.values, r.label);
    const cls = [r.role === "total" ? "tot" : "", c ? "flagged" : ""].join(" ").trim();
    const cells = Array.from({ length: nv }, (_, i) => `<td class="n">${fmtKr(r.values[i])}</td>`).join("");
    return `<tr class="${cls}"><td class="lbl">${esc(r.label)}${why(c)}</td>${cells}</tr>`;
  }).join("");
  return `<div class="tw"><table>${head}${body}</table></div>`;
}

/** Text and tables in printed order, with a marker where the content moves to the next page. */
export function flow(seq, checks, skipTitle = true) {
  let h = "", last = seq.length ? seq[0].page : null, list = false;
  const closeList = () => { if (list) { h += "</ul>"; list = false; } };
  seq.forEach((s, i) => {
    if (s.t === "text" && s.kind === "heading" && i === 0 && skipTitle && /^note\s/i.test(s.text)) return;
    if (s.page !== last) { closeList(); h += `<div class="pbreak"><button type="button" class="lk pg" data-page="${s.page}">side ${s.page}</button></div>`; last = s.page; }
    if (s.t === "text" && s.kind === "list_item") { if (!list) { h += `<ul class="tx">`; list = true; } h += `<li data-page="${s.page}">${esc(s.text)}</li>`; return; }
    closeList();
    if (s.t === "table") h += `<div class="tblock" data-page="${s.page}">${noteTable(s.table, s.page, checks)}</div>`;
    else if (s.kind === "heading") h += `<h4 class="tx" data-page="${s.page}">${esc(s.text)}</h4>`;
    else h += `<p class="tx" data-page="${s.page}">${esc(s.text)}</p>`;
  });
  closeList();
  return h;
}

function checkList(checks, cls = "") {
  return `<ul class="chk-list${cls ? ` ${cls}` : ""}">${checks.map((c) => `<li data-page="${c.page ?? ""}"><span class="k">${esc(c.kind)}</span><span>${esc(c.text)}${c.page ? ` <span class="pg">s. ${c.page}</span>` : ""}</span></li>`).join("")}</ul>`;
}

function inlineList(items) {
  return `<details class="nums"><summary>Tall i teksten (${items.length})</summary><ul class="inl">${items.map((a) =>
    `<li data-page="${a.page}"><span>${esc(a.context)}</span><span class="num">${fmtKr(a.value)}</span><span class="pg">s. ${a.page}</span></li>`).join("")}</ul></details>`;
}

// Last year's column next to last year's own filing: information, never a flag (figures can be restated).
const INFO_NOTE = "Fjorårstallene er sammenlignet med fjorårets regnskap. Et avvik betyr ikke at noe er lest feil: selskaper kan omarbeide fjorårstallene, og da står det gjerne i notene.";
const infoFor = (info, l) => (info || []).find((x) => x.page === l.page && x.row === l.label);

function statementHtml(st, checks, hl, info = []) {
  const showAlso = st.copy === "company_copy" && st.lines.some((l) => l.also);
  const unit = st.unit_scale === 1000 ? "Beløp i hele tusen" : st.unit_scale === 1000000 ? "Beløp i millioner" : "Beløp i kroner";
  const rows = st.lines.map((l, i) => {
    if (l.cur === null && l.prior === null) return `<tr class="head"><td colspan="${showAlso ? 4 : 3}">${esc(l.label)}</td></tr>`;
    const c = flagFor(checks, l.page, [l.cur, l.prior], l.label);
    const sum = l.key || /^sum|resultat$/i.test(l.label);
    const cls = [sum ? "tot" : "", c ? "flagged" : "", hl === i ? "hl" : ""].join(" ").trim();
    return `<tr data-page="${l.page}" data-row="${i}" class="${cls}"><td class="lbl">${esc(l.label)}${l.ref ? `<button type="button" class="ref lk" data-note="${esc(l.ref)}">note ${esc(l.ref)}</button>` : ""}${why(c)}</td>
      <td class="n">${fmtKr(l.cur)}</td><td class="n">${fmtKr(l.prior)}${infoFor(info, l) ? `<span class="inf" title="${esc(infoFor(info, l).text)}">≠ i fjor</span>` : ""}</td>${showAlso ? `<td class="n chk" title="${l.also ? `Samme tall i Brregs utskrift, side ${l.also}` : "Bare i selskapets oppstilling"}">${l.also ? `✓ s. ${l.also}` : ""}</td>` : ""}</tr>`;
  }).join("");
  const both = st.lines.filter((l) => l.also).length, all = st.lines.filter((l) => l.cur !== null).length;
  const mine = (info || []).filter((x) => st.lines.some((l) => l.page === x.page && l.label === x.row));
  const src = st.copy === "brreg_printout" ? `Brregs utskrift ${pageSpan(st.pages)}` : `Årsrapportens oppstilling ${pageSpan(st.pages)}${showAlso ? ` · ${both} av ${all} linjer også i Brregs utskrift` : ""}`;
  return `<div class="sec-h"><h2>${esc(st.title)}</h2><span class="muted">${src} · ${unit}</span></div>
    <div class="tw"><table><tr><th>Linje</th><th class="n">${st.current_year ?? "I år"}</th><th class="n">${st.prior_year ?? "I fjor"}</th>${showAlso ? `<th class="n" title="Samme tall står i Brønnøysundregistrenes egen utskrift i PDF-en">Brregs utskrift</th>` : ""}</tr>${rows}</table></div>
    ${mine.length ? `<p class="intro">${INFO_NOTE}</p>${checkList(mine, "info")}` : ""}`;
}

/** The middle column for section `cur`. */
export function mainHtml(d, secs, cur, hl) {
  const s = secs.find((x) => x.id === cur);
  if (cur === "checks") {
    const info = d.info || [];
    return `<div class="sec-h"><h2>Til kontroll</h2><span class="muted">${nCheck(d.checks.length)}</span></div>
      <p class="intro">Tallene er kontrollert automatisk: summer, at balansen går opp, at Brregs utskrift og selskapets egen oppstilling er like, og at årets tall stemmer med Brregs tall der Brreg har dem.
      ${d.checks.length ? "Under er det som ikke kunne bekreftes. Klikk for å se siden." : "Alt som ble kontrollert, stemmer."}</p>${checkList(d.checks)}
      ${info.length ? `<h3 class="info-h">Til informasjon (${info.length})</h3><p class="intro">${INFO_NOTE}</p>${checkList(info, "info")}` : ""}`;
  }
  if (STMT.includes(cur)) return statementHtml(d.statements[cur], d.checks, hl, d.info);
  if (cur.startsWith("k-")) return statementHtml({ ...d.konsern[cur.slice(2)], title: `${d.konsern[cur.slice(2)].title} · konsern` }, d.checks, hl, d.info);
  if (!s) return "";
  const pageChecks = s.checks;
  if (["beretning", "revisjon", "annet"].includes(cur)) {
    const seq = d.misc.filter((m) => s.pages.includes(m.page)).flatMap((m) => m.seq);
    const items = d.inline.filter((a) => s.pages.includes(a.page));
    return `<div class="sec-h"><h2>${esc(s.title)}</h2><span class="muted">${pageSpan(s.pages)}</span></div>
      ${pageChecks.length ? checkList(pageChecks) : ""}<div class="flow">${flow(seq, d.checks, false)}</div>${items.length ? inlineList(items) : ""}`;
  }
  const n = d.notes[Number(cur.slice(1))];
  const items = d.inline.filter((a) => a.kind === "noter" && n.pages.includes(a.page));
  const body = n.seq.length ? flow(n.seq, d.checks) : `<p class="intro">Ingen tekst eller tabeller lest ut. Se skannet.</p>`;
  return `<div class="sec-h"><h2>${esc(noteLabel(n))} · ${esc(n.title)}</h2><span class="muted">${pageSpan(n.pages)}</span></div>
    ${pageChecks.length ? checkList(pageChecks) : ""}<div class="flow">${body}</div>${items.length ? inlineList(items) : ""}`;
}

/** What page `n` shows, in words: its notes, else the first part showing it, else its kind (Brreg's printout pages
 * aren't parts). */
export function pageTitle(d, secs, n) {
  const p = d.pages.find((x) => x.n === n) || { kind: "annet", printout: false, for: "selskap" };
  const pre = (s) => (s.group === "Konsern" ? "Konsern · " : "");
  const notes = secs.filter((s) => s.id[0] === "n" && s.pages.includes(n));
  if (notes.length > 1) {
    const nums = [...new Set(notes.map((s) => s.num).filter(Boolean))];
    const ns = nums.length > 3 ? `${nums[0]}–${nums[nums.length - 1]}` : nums.join(", ");
    return `${pre(notes[0])}Noter${ns ? ` ${ns}` : ""}`;
  }
  const s = notes[0] || secs.find((x) => x.pages.includes(n));
  if (s) return pre(s) + (s.num ? `Note ${s.num} ${s.title}` : s.title);
  return (p.printout ? "Brregs utskrift · " : "") + (p.for === "konsern" ? "Konsern · " : "") + (KIND[p.kind] || p.kind);
}

/** The pages with something to check before and after `page`, and `page`'s place among them (0: none on it). */
export function checkStep(d, page) {
  const fp = [...new Set(d.checks.map((c) => c.page).filter(Boolean))].sort((a, b) => a - b);
  return {
    prev: [...fp].reverse().find((p) => p < page) ?? null,
    next: fp.find((p) => p > page) ?? null,
    at: fp.indexOf(page) + 1,
    total: fp.length,
  };
}

/** Every page with what it shows and its checks, kept when `filter` is its number or in the title of any part on it
 * (a page of several notes is titled "Noter 7, 8, 9" but found by "skatt"). */
export function pageListHtml(d, secs, page, filter = "") {
  const q = filter.trim().toLowerCase();
  const counts = {};
  for (const c of d.checks) if (c.page) counts[c.page] = (counts[c.page] || 0) + 1;
  const words = (n, t) => [t, ...secs.filter((s) => s.pages.includes(n)).map((s) => s.title)].join(" ").toLowerCase();
  const rows = d.pages.map((p) => ({ n: p.n, t: pageTitle(d, secs, p.n), f: counts[p.n] || 0 }))
    .filter((r) => !q || String(r.n) === q || words(r.n, r.t).includes(q));
  if (!rows.length) return `<li class="none">Ingen sider passer «${esc(filter.trim())}».</li>`;
  return rows.map((r) => `<li><button type="button" role="option" data-page="${r.n}" aria-current="${r.n === page}">
    <span class="pn">${r.n}</span><span class="pt">${esc(r.t)}</span><span class="pf">${r.f ? nCheck(r.f) : ""}</span></button></li>`).join("");
}

/** First section showing note `ref` ("3, 4" → note 3). */
export function noteSection(d, ref) {
  const no = String(ref).split(",")[0].trim();
  const i = d.notes.findIndex((n) => n.number === no);
  return i < 0 ? null : `n${i}`;
}

/** Search hit snippet: escaped text with the FTS match markers (\x01 … \x02) as <mark>. */
export function markSnippet(s) {
  return esc(s).replace(/\x01/g, "<mark>").replace(/\x02/g, "</mark>");
}

/** The part of a read year that shows `page`; the hit's note decides when a page holds several notes. */
export function sectionForPage(secs, d, page, note = null) {
  const notes = secs.filter((s) => s.id[0] === "n" && s.pages.includes(page));
  return (note && notes.find((s) => d.notes[Number(s.id.slice(1))].number === note))
    || notes[0] || secs.find((s) => s.pages.includes(page)) || null;
}

const where = (h) => `${h.year} · s. ${h.page}${h.note ? ` · note ${esc(h.note)}` : ""}`;

/** Results of GET /accounts/search across the company's read years. */
export function searchHtml(res) {
  const { text, figures, q } = res;
  if (!text.length && !figures.length) return `<div class="sec-h"><h2>Søk</h2></div><p class="intro">Fant ikke «${esc(q)}» i årene som er lest inn.</p>`;
  const t = text.map((h) => `<li data-hit-year="${h.year}" data-hit-page="${h.page}" data-hit-note="${esc(h.note ?? "")}">
      <span class="hit-where">${where(h)}</span><span class="hit-text">${markSnippet(h.snippet)}</span></li>`).join("");
  const f = figures.map((h) => `<tr data-hit-year="${h.year}" data-hit-page="${h.page}" data-hit-note="${esc(h.note ?? "")}">
      <td class="lbl">${esc(h.label)}${h.col && !["current", "prior"].includes(h.col) ? ` <span class="muted">· ${esc(h.col)}</span>` : ""}${h.figures_for === "konsern" ? ' <span class="muted">· konsern</span>' : ""}</td>
      <td class="n">${fmtKr(h.value)}</td><td class="n pg">${where(h)}</td></tr>`).join("");
  return `<div class="sec-h"><h2>Søk: «${esc(q)}»</h2><span class="muted">${text.length} i teksten · ${figures.length} tall</span></div>
    ${text.length ? `<h3 class="hit-h">I teksten</h3><ul class="hits">${t}</ul>` : ""}
    ${figures.length ? `<h3 class="hit-h">Tall</h3><div class="tw"><table class="hit-figs"><tr><th>Linje</th><th class="n">Beløp</th><th class="n">Hvor</th></tr>${f}</table></div>` : ""}`;
}

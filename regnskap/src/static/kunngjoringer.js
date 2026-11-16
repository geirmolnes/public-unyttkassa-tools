// Kunngjøringer on the company page (GET /api/company/{orgnr}/kunngjoringer), without DOM: what each notice
// says in one line, what each topic is now and has been, bankruptcy facts, search, a notice in full. app.js
// fetches and draws; tests/js/kunngjoringer.test.js runs this under bun.
import { esc, plural } from "./format.js";

export const shortDate = (iso) => { const [y, m, d] = iso.split("-"); return `${d}.${m}.${y}`; };

const field = (it, label) => (it.parts || []).find((p) => p[0] === "f" && p[1].toLowerCase() === label.toLowerCase());
const value = (it, label) => { const f = field(it, label); return f ? f[2] : ""; };
const firstLine = (v) => (v || "").split("\n")[0];
// «Org.nr 935 416 779 CEDRA NORGE AS, Ruseløkkveien 30, 0251 OSLO» → «CEDRA NORGE AS»
const auditorName = (v) => firstLine(v).replace(/^Org\.nr [\d ]{11}\s*/, "").split(",")[0];
const personName = (v) => firstLine(v).split(",")[0];
const isNew = (it) => /nyregistrering/i.test(it.title || it.type);
// Brreg prints the role's new value; for a removed role that's a word, not a name.
const ENDED = /^(utgår|utgått|fratrådt|avregistrert|slettet)$/i;
const ROLE_NOUN = { "Daglig leder": "daglig leder", "Revisor": "revisor", "Styrets leder": "styreleder", "Regnskapsfører": "regnskapsfører" };

/** A role field as {v, ev}: the holder's name, or «Ingen daglig leder» + the event when the role was removed. */
function role(it, label, name) {
  const raw = value(it, label).trim();
  if (!raw) return null;
  if (ENDED.test(raw)) return { v: `Ingen ${ROLE_NOUN[label]}`, ev: raw.toLowerCase().replace("utgår", "utgått") };
  return { v: name(raw) };
}

/** The notice's title without «Endring av», which half of them start with. Unread: Brreg's type. */
export const shortTitle = (it) => (it.title || it.type).replace(/^Endring av /i, "").replace(/^./, (c) => c.toUpperCase());

const clip = (s, n) => (s.length > n ? `${s.slice(0, n - 2)} …` : s);

/** In a merger or demerger: [«fra»/«til», company name] per «Overdragende/Overtakende …» heading. */
function mergerRoles(it) {
  const out = [];
  let role = null;
  for (const p of it.parts || []) {
    if (p[0] === "h") role = /overdragende/i.test(p[1]) ? "fra" : /overtakende/i.test(p[1]) ? "til" : null;
    else if (role && p[0] === "f" && /foretaksnavn/i.test(p[1])) { out.push([role, p[2]]); role = null; }
  }
  return out.sort((a, b) => (a[0] === "fra" ? -1 : 1) - (b[0] === "fra" ? -1 : 1));
}

/** What was announced in one line, as {v, ev} (ev: an event word such as «utgått»), or null. */
export function summary(it) {
  if (!it.read) return null;
  const text = (it.parts || []).map((p) => p.slice(1).join(" ")).join(" ");
  if (/årsregnskap/i.test(it.title)) {
    const year = text.match(/regnskapsåret (\d{4})/), got = text.match(/har den (\d\d\.\d\d\.\d{4})/);
    return { v: [year && `For ${year[1]}`, got && `mottatt ${got[1]}`].filter(Boolean).join(", ") };
  }
  if (isNew(it)) {
    return { v: [value(it, "Kapital"), value(it, "Daglig leder") && `daglig leder ${personName(value(it, "Daglig leder"))}`].filter(Boolean).join(" · ") };
  }
  for (const [label, name] of [["Revisor", auditorName], ["Daglig leder", personName]]) {
    const r = role(it, label, name);
    if (r) return r;
  }
  if (value(it, "Styrets leder")) {
    const r = role(it, "Styrets leder", personName);
    const members = value(it, "Styremedlem").split("\n").filter((x) => x && !/^Representant|^Avregistrert/.test(x)).length;
    return { v: `${r.ev ? r.v : `Leder ${r.v}`}${members ? ` · ${plural(members, "medlem", "medlemmer")}` : ""}`, ev: r.ev };
  }
  if (value(it, "Konkurs åpnet")) return { v: `Åpnet ${value(it, "Konkurs åpnet")}${value(it, "Åpnet etter") ? ` etter ${value(it, "Åpnet etter").toLowerCase()}` : ""}` };
  if (value(it, "Ny forretningsadresse")) return { v: value(it, "Ny forretningsadresse").replace(/\n/g, ", ") };
  if (value(it, "Nytt foretaksnavn")) return { v: value(it, "Nytt foretaksnavn") };
  if (value(it, "Kapital")) return { v: value(it, "Kapital") };
  const most = value(it, "Maks. kapitalforhøyelse") || value(it, "Maks. pålydende verdi");
  if (most) return { v: `Inntil ${most}${value(it, "Dato utgår") ? `, til ${value(it, "Dato utgår")}` : ""}` };
  if (value(it, "Vedtektsdato")) return { v: `Vedtekter av ${value(it, "Vedtektsdato")}` };
  if (value(it, "Signatur")) return { v: value(it, "Signatur").replace(/\n/g, " ") };
  if (value(it, "Formål")) return { v: clip(value(it, "Formål"), 90) };
  const roles = mergerRoles(it);
  if (roles.length) return { v: roles.map(([r, name]) => `${r} ${name}`).join(" · ") };
  const p = (it.parts || []).find((x) => x[0] !== "f");
  if (p) return { v: clip(p[1], 100) };
  const f = (it.parts || []).find((x) => x[0] === "f" && x[2]);
  return f ? { v: `${f[1]}: ${firstLine(f[2])}` } : null;
}

/** {v, ev} as HTML: an event reads «Ingen daglig leder» + a small «utgått» label. */
export const valHtml = (x) => (!x ? "" : x.ev ? `${esc(x.v)} <span class="kn-tag">${esc(x.ev)}</span>` : esc(x.v));

// [key, heading, which notices (type + title), what each says the topic is now: {v, ev}]
const TRACKS = [
  ["revisor", "Revisor", /revisor|nyregistrering/i,
    (it) => role(it, "Revisor", auditorName) || (/ikke revideres/i.test(value(it, "Revisjon av årsregnskap")) && { v: "Ingen revisor", ev: "fravalgt" })],
  ["styre", "Styreleder", /styre|nyregistrering/i, (it) => role(it, "Styrets leder", personName)],
  ["dl", "Daglig leder", /daglig leder|nyregistrering/i, (it) => role(it, "Daglig leder", personName)],
  ["kapital", "Aksjekapital", /kapital|nyregistrering/i, (it) => value(it, "Kapital") && { v: value(it, "Kapital") }],
  ["adresse", "Forretningsadresse", /adresse|nyregistrering/i, (it) => {
    const a = (value(it, "Ny forretningsadresse") || (isNew(it) ? it.address || "" : "")).replace(/\n/g, ", ");
    return a && { v: a };
  }],
  ["navn", "Navn", /navn|firma|nyregistrering/i, (it) => {
    const n = value(it, "Nytt foretaksnavn") || (isNew(it) ? it.name || "" : "");
    return n && { v: n };
  }],
  ["fullmakt", "Fullmakter", /fullmakt|erverv/i,
    (it) => ({ v: `${shortTitle(it).replace(/^Fullmakt /, "").replace(/^./, (c) => c.toUpperCase())}${value(it, "Dato utgår") ? `, til ${value(it, "Dato utgår")}` : ""}` })],
  ["regnskap", "Årsregnskap", /årsregnskap/i, (it) => { const s = summary(it); return s && s.v && { v: s.v.replace(/^For /, "") }; }],
  ["konkurs", "Konkurs og avvikling", /konkurs|tvangsoppl|avvikl|oppløs|sletting|akkord/i,
    (it) => ({ v: `${shortTitle(it)}${summary(it) ? `: ${summary(it).v}` : ""}` })],
];
const inTrack = (re, it) => re.test(`${it.type} ${it.title || ""}`);

/** Per topic {key, label, xs, all, unread}: `xs` its values newest first ({it, v, ev}; a value that didn't change
 * stays with the notice that set it), `all` every notice of that kind, read or not. Topics with nothing are left
 * out; «Annet» (other: true) collects the notices that fit none. */
export function tracks(items) {
  const out = TRACKS.map(([key, label, re, val]) => {
    const all = items.filter((it) => inTrack(re, it));
    const xs = all.filter((it) => it.read).map((it) => ({ it, ...(val(it) || {}) })).filter((x) => x.v);
    const kept = xs.reverse().filter((x, i, a) => i === 0 || x.v !== a[i - 1].v).reverse();
    return { key, label, xs: kept, all, unread: all.filter((it) => !it.read).length };
  }).filter((t) => t.xs.length || t.unread);
  const other = items.filter((it) => !TRACKS.some(([, , re]) => inTrack(re, it)));
  if (other.length) {
    out.push({ key: "annet", label: "Annet", other: true, xs: [], all: other, unread: other.filter((it) => !it.read).length });
  }
  return out;
}

/** The newest bankruptcy or forced dissolution notice (an unread one is known by its type). */
export const bankruptcy = (items) => items.find((it) => /konkurs|tvangsoppl/i.test(`${it.type} ${it.title || ""}`));

/** «Konkurs åpnet 18.09.2026» rather than Brreg's «Konkurs - åpning». */
export const bankruptcyHead = (it) =>
  `${/konkurs.*åpning|konkursåpning/i.test(`${it.type} ${it.title || ""}`) ? "Konkurs åpnet" : shortTitle(it)} ${shortDate(it.date)}`;

/** [label, value] for the bankruptcy banner: grounds, court, trustee, claim deadline, creditors' meeting. */
export function bankruptcyFacts(it) {
  if (!it.read) return [];
  const text = (it.parts || []).map((p) => p.slice(1).join(" ")).join(" ");
  const court = text.match(/Ved (.+?) er det åpnet/);
  const trustee = text.match(/meldes bostyrer,? ([^,]+)/);
  const due = text.match(/innen (\d\d\.\d\d\.\d{4})/), meet = text.match(/skiftesamling blir holdt (\d\d\.\d\d\.\d{4})/);
  const cap = (s) => s.toLowerCase().replace(/(^|\s)\S/g, (c) => c.toUpperCase());
  return [
    value(it, "Åpnet etter") && ["Åpnet etter", value(it, "Åpnet etter")],
    court && ["Domstol", cap(court[1])],
    trustee && ["Bostyrer", trustee[1]],
    due && ["Krav innen", due[1]],
    meet && ["Skiftesamling", meet[1]],
  ].filter(Boolean);
}

/** Notices holding every word of `q`: read ones by their whole text, unread ones only by type and date. */
export function search(items, q) {
  const words = q.toLocaleLowerCase("nb").split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  return items.filter((it) => {
    const hay = [it.type, it.title, it.date, shortDate(it.date), ...(it.parts || []).map((p) => p.slice(1).join(" "))].join(" ").toLocaleLowerCase("nb");
    return words.every((w) => hay.includes(w));
  });
}

/** A read notice in full: headings, paragraphs, label/value rows (a label without a value heads a group). */
export function noticeHtml(it) {
  const out = [];
  let dl = false;
  for (const p of it.parts || []) {
    if (p[0] === "f") {
      if (!dl) { out.push('<dl class="kn-f">'); dl = true; }
      const ended = p[2] && ENDED.test(p[2].trim());
      out.push(!p[2] ? `<dt class="grp">${esc(p[1])}</dt>`
        : `<dt>${esc(p[1])}</dt><dd>${ended ? `<span class="kn-tag">${esc(p[2])}</span> rollen er fjernet` : esc(p[2])}</dd>`);
      continue;
    }
    if (dl) { out.push("</dl>"); dl = false; }
    out.push(`<p${p[0] === "h" ? ' class="kn-h"' : ""}>${esc(p[1])}</p>`);
  }
  if (dl) out.push("</dl>");
  const then = [it.name, it.address].filter(Boolean).join(" · ");
  return `<div class="kn-notice">${out.join("")}${then ? `<p class="kn-meta">Foretaket da: ${esc(then)}</p>` : ""}
    <p class="kn-meta">${esc(it.register || "")} ${shortDate(it.date)} · <a href="${esc(it.url)}" target="_blank" rel="noopener">Åpne hos Brreg ↗</a></p></div>`;
}

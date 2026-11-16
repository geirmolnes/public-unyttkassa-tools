// Årsregnskap UI: sidebar nav (favourites, collections, recent) · company view · ZIP jobs · AI chat.
import { esc, fmtInt, fmtPct, fmtThousands, nf0, norwegian, plural, recentFirst } from "./format.js";
import { clampView, fitView, layoutMap, mergeFollow, stakeLabel, treeIndex } from "./eierkart.js";
import { bankruptcy, bankruptcyFacts, bankruptcyHead, noticeHtml, search as searchNotices, shortDate, shortTitle, summary, tracks, valHtml } from "./kunngjoringer.js";
import { searchGroups } from "./search.js";
import { checkStep, mainHtml, noteSection, pageListHtml, pageTitle, searchHtml, sectionForPage, sections, tocHtml } from "./reader.js";
import { BALANCE_ROWS, EK_ROW, FIELD, RESULT_ROWS, busyRow, controlYears, exportFilename, failReason, exportSheets, flagNote, mergeHistory, notFound, progressText, sourceLabel, spanLabel, toTsv, unreadYears, visibleRows, yearStatus } from "./figures.js";

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const JOB_POLL_MS = 600;
  const SUBS_POLL_MS = 3000;
  const SUBS_PREVIEW = 10;
  const VIEWS = ["home", "company", "person", "people", "structure", "accounts", "favs", "collections", "collection", "batch"];
  const ACC_POLL_MS = 2000;

  const state = {
    view: "home",
    data: null,          // GET /api/company payload for the open company
    orgnr: null,         // company being opened/shown
    pick: [],            // years ticked under Last ned › Velg år
    pickOpen: false,
    anchor: null,
    subsQuery: "",
    subsAll: false,
    subsTimer: null,
    favs: [],            // [{orgnr, name}] from /api/favourites
    favRows: [],         // Row[] from /api/favourites?rows=true, for the Favoritter table
    colls: [],           // [{id, name, count, preview}]
    recent: [],
    unrecent: new Set(), // removed with ×: a GET /api/recent that raced the DELETE mustn't bring them back
    collId: null,
    coll: null,          // {id, name, items}
    picked: new Set(),
    rule: "latest",
    confirmDel: false,
    jobTimer: null,
    jobRunning: false,
    turns: [],
    chatBusy: false,
    figYear: null,       // year shown in the figures; null = newest
    figScope: "selskap", // "selskap" | "konsern" accounts
    figView: null,       // "year" | "all" (every year Brreg has as data, one table), remembered per browser
    figRange: null,      // "all" | "3" | "5" | "10": years in the all-years table once there are more than 3, remembered
    figOff: 0,           // newest years skipped when figRange limits the table (arrows move it)
    readFig: null,       // GET /api/company/{orgnr}/accounts/figures: years read from PDFs, per scope
    reg: null,           // /api/company/{orgnr}/shareholders snapshot, or {error}
    regSeq: 0,
    kn: null,            // kunngjøringer for state.orgnr: {orgnr, d, error, open, more, all, q, busy}
    person: null,        // {name, born, data, histOpen} for the person view
    people: null,        // {q, data, filter} for the list of people matching a search
    struct: null,        // structure view: {arg, data, open, upOpen, q, mode, level, ...}
    accRows: [],         // read status per year for state.orgnr (GET /api/company/{orgnr}/accounts)
    accTimer: null,
    acc: null,           // reader view: {orgnr, year, d, secs, cur, page, hl}
  };

  // ---------- helpers ----------

  const fmtOrgnr = (o) => (o && /^\d{9}$/.test(o) ? `${o.slice(0, 3)} ${o.slice(3, 6)} ${o.slice(6)}` : o || "");
  const nf1 = new Intl.NumberFormat("nb-NO", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const minus = (s) => s.replace("-", "−");

  function storeGet(key, fallback) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; }
  }
  function storeSet(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* storage unavailable */ }
  }

  const k = fmtThousands;
  /** Negated amount in 1 000 units (costs shown as negative). */
  const kNeg = (v) => (v === null || v === undefined ? "–" : k(-Math.abs(v)));

  function short(v, currency) {
    if (v === null || v === undefined) return "–";
    const abs = Math.abs(v);
    const cur = currency && currency !== "NOK" ? ` ${currency}` : "";
    if (abs >= 1e9) return minus(`${nf1.format(v / 1e9)} mrd${cur}`);
    if (abs >= 1e6) return minus(`${nf1.format(v / 1e6)} mill${cur}`);
    return minus(`${nf0.format(v)} kr${cur}`);
  }
  const pct = (x) => (x === null || x === undefined ? "–" : `${nf1.format(x * 100)} %`);
  const ddmm = (iso) => (iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.` : "");

  async function apiError(resp) {
    try {
      const body = await resp.json();
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) return "Ugyldig forespørsel.";
    } catch (e) { /* not JSON */ }
    if (resp.status >= 500) return "Noe gikk galt hos oss eller hos Brreg. Prøv igjen.";
    return `Feil (${resp.status}).`;
  }

  // Errors the browser hits go to the server's event log (/api/diagnostics), so they can be traced afterwards.
  // Each distinct message once per page load, at most REPORT_MAX.
  const REPORT_MAX = 30;
  const reported = new Set();
  function report(kind, message, stack) {
    const key = `${kind}:${message}`;
    if (reported.has(key) || reported.size >= REPORT_MAX) return;
    reported.add(key);
    const body = { kind, message: String(message).slice(0, 2000), stack: stack ? String(stack).slice(0, 8000) : null,
      view: state.view, url: location.href.slice(0, 1000), orgnr: state.orgnr };
    fetch("/api/client-log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), keepalive: true })
      .catch(() => { /* nothing more to do */ });
  }
  window.addEventListener("error", (e) => report("js", e.message, e.error && e.error.stack));
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason || {};
    report("promise", r.message || String(e.reason), r.stack);
  });

  async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      report("network", `${method} ${url}: ${e.message}`);
      throw e;
    }
    if (!resp.ok) {
      const err = new Error(await apiError(resp));
      err.status = resp.status;
      if (resp.status >= 500) report("api", `${method} ${url} → ${resp.status}: ${err.message}`);
      throw err;
    }
    return resp.status === 204 ? null : resp.json();
  }

  function filenameFrom(resp, fallback) {
    const cd = resp.headers.get("Content-Disposition") || "";
    const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
    if (star) return decodeURIComponent(star[1]);
    const plain = cd.match(/filename="([^"]+)"/i);
    return plain ? plain[1] : fallback;
  }

  function triggerDownload(href, filename) {
    const a = document.createElement("a");
    a.href = href;
    a.download = filename || "";
    document.body.append(a);
    a.click();
    a.remove();
  }

  function setStatus(text, isErr) {
    if (isErr && text) report("shown", text);
    $("status-text").textContent = text || "";
    $("status").hidden = !text;
    $("status").classList.toggle("err", !!isErr);
  }
  $("status-x").addEventListener("click", () => setStatus(""));

  function badges(c) {
    const out = [];
    if (c.kind === "slettet") out.push(["Slettet" + (c.deleted_date ? " " + c.deleted_date.slice(0, 4) : ""), "badge-red"]);
    if (c.konkurs) out.push(["Konkurs", "badge-red"]);
    if (c.under_avvikling) out.push(["Under avvikling", "badge-amber"]);
    if (c.kind === "underenhet") out.push(["Underenhet", "badge-amber"]);
    return out.map(([t, cls]) => `<span class="badge ${cls}">${esc(t)}</span>`).join("");
  }

  // ---------- theme ----------

  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
    document.querySelectorAll("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === t)));
  }
  document.querySelectorAll("[data-theme-set]").forEach((b) => b.addEventListener("click", () => {
    storeSet("theme", b.dataset.themeSet);
    applyTheme(b.dataset.themeSet);
  }));
  applyTheme(storeGet("theme", "system"));

  // ---------- routing ----------

  function urlFor(view, arg) {
    if (view === "company" && arg) return `/?orgnr=${arg}`;
    if (view === "person" && arg) return `/?eier=${encodeURIComponent(arg.name)}${arg.born ? `&f=${arg.born}` : ""}`;
    if (view === "structure" && arg && arg.person) return `/?eierstruktur=${encodeURIComponent(arg.person)}${arg.born ? `&f=${arg.born}` : ""}${arg.year ? `&aar=${arg.year}` : ""}`;
    if (view === "structure" && arg) return `/?eierkart=${arg.orgnr}${arg.year ? `&aar=${arg.year}` : ""}`;
    if (view === "accounts" && arg) return `/?regnskap=${arg.orgnr}&aar=${arg.year}`;
    if (view === "people" && arg) return `/?personer=${encodeURIComponent(arg.q)}`;
    if (view === "favs") return "/?side=favoritter";
    if (view === "collections") return "/?side=samlinger";
    if (view === "collection") return `/?samling=${arg}`;
    if (view === "batch") return "/?modus=batch";
    return "/";
  }

  function showView(view) {
    state.view = view;
    VIEWS.forEach((v) => { $("v-" + v).hidden = v !== view; });
    // The eierkart and the regnskapsleser take the whole window: no sidebar, top search or chat.
    $("app").classList.toggle("full", view === "structure" || view === "accounts");
    if (!state.jobRunning) $("job").hidden = true;
    closeCollPop();
    if (view !== "company") stopSubsPoll();
    renderNav();
    renderChatContext();
  }

  /** Navigate within the app; `push` adds a history entry. */
  function go(view, arg, push = true) {
    if (push) history.pushState({}, "", urlFor(view, arg));
    setStatus("");
    window.scrollTo({ top: 0 });
    if (view === "company" && arg) return openCompany(arg);
    if (view === "person" && arg) return openPerson(arg);
    if (view === "structure" && arg) return openStructure(arg);
    if (view === "accounts" && arg) return openAccounts(arg);
    if (view === "people" && arg) return openPeople(arg);
    if (view === "company" && !arg && state.data) return openCompany(state.data.company.orgnr);
    if (view === "company") view = "home";
    showView(view);
    if (view === "home") renderHome();
    if (view === "favs") loadFavs();
    if (view === "collections") loadCollections();
    if (view === "collection") { state.picked = new Set(); state.confirmDel = false; loadCollection(arg); }
    if (view === "batch") $("batch-input").focus();
    document.title = "Årsregnskap · unyttkassa";
  }

  function route() {
    const p = new URLSearchParams(location.search);
    const orgnr = (p.get("orgnr") || "").replace(/\s+/g, "");
    if (p.get("modus") === "batch") {
      go("batch", null, false);
      if (orgnr) { $("batch-input").value = (p.get("orgnr") || "").split(",").join("\n"); renderParsed(); }
      return;
    }
    if (/^\d{9}$/.test(orgnr)) return go("company", orgnr, false);
    const aar = /^\d{4}$/.test(p.get("aar") || "") ? Number(p.get("aar")) : null;
    if (/^\d{9}$/.test(p.get("eierkart") || "")) return go("structure", { orgnr: p.get("eierkart"), year: aar }, false);
    if (p.get("eierstruktur")) {
      return go("structure", { person: p.get("eierstruktur"), born: /^\d{4}$/.test(p.get("f") || "") ? Number(p.get("f")) : null, year: aar }, false);
    }
    if (/^\d{9}$/.test(p.get("regnskap") || "") && /^\d{4}$/.test(p.get("aar") || "")) {
      return go("accounts", { orgnr: p.get("regnskap"), year: Number(p.get("aar")) }, false);
    }
    if ((p.get("personer") || "").trim().length >= 2) return go("people", { q: p.get("personer").trim() }, false);
    if (p.get("eier")) return go("person", { name: p.get("eier"), born: /^\d{4}$/.test(p.get("f") || "") ? Number(p.get("f")) : null }, false);
    if (p.get("side") === "favoritter") return go("favs", null, false);
    if (p.get("side") === "samlinger") return go("collections", null, false);
    if (/^\d+$/.test(p.get("samling") || "")) return go("collection", Number(p.get("samling")), false);
    go("home", null, false);
    $("search-input").focus();
  }
  window.addEventListener("popstate", route);

  document.addEventListener("click", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.target.closest("a[href]")) return; // let the browser open a new tab
    const open = e.target.closest("[data-open]");
    if (open) { e.preventDefault(); go("company", open.dataset.open); return; }
    const person = e.target.closest("[data-person]");
    if (person) { e.preventDefault(); go("person", { name: person.dataset.person, born: person.dataset.born ? Number(person.dataset.born) : null }); return; }
    const map = e.target.closest("[data-structure]");
    if (map) { e.preventDefault(); go("structure", { orgnr: map.dataset.structure, year: map.dataset.year ? Number(map.dataset.year) : null }); return; }
    const pmap = e.target.closest("[data-pstructure]");
    if (pmap) {
      e.preventDefault();
      go("structure", { person: pmap.dataset.pstructure, born: pmap.dataset.born ? Number(pmap.dataset.born) : null, year: pmap.dataset.year ? Number(pmap.dataset.year) : null });
      return;
    }
    const nav = e.target.closest("[data-nav]");
    if (nav) { e.preventDefault(); go(nav.dataset.nav); return; }
    const coll = e.target.closest("[data-coll]");
    if (coll) { e.preventDefault(); go("collection", Number(coll.dataset.coll)); return; }
    const pdf = e.target.closest("[data-pdf]");
    if (pdf) { e.preventDefault(); downloadSingle(pdf.dataset.pdf, Number(pdf.dataset.year)); return; }
    if (!e.target.closest(".head-btns")) closeCollPop();
  });

  // "/" search, "k" chat. On the eierkart: Esc closes help, then clears the search, then goes
  // back to the company; + − 0 zoom.
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable) return;
    const map = state.view === "structure";
    const s = map ? mapState() : null;
    if (e.key === "/") { e.preventDefault(); const box = map ? $("s-q") : state.view === "accounts" ? $("a-q") : $("search-input"); box.focus(); box.select(); }
    else if (map && e.key === "Escape" && state.struct) {
      e.preventDefault();
      if (!$("s-help-pop").hidden) setMapHelp(false);
      else if (s && s.q) clearMapSearch(s);
      else $("s-back").click();
    }
    else if (s && (e.key === "+" || e.key === "=")) zoomMid(s, 1.25);
    else if (s && e.key === "-") zoomMid(s, 0.8);
    else if (s && e.key === "0") fitStructure(s);
    else if (state.view === "accounts" && state.acc && state.acc.d && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      e.preventDefault();
      showPage(state.acc.page + (e.key === "ArrowRight" ? 1 : -1));
    }
    else if (state.view === "accounts" && e.key === "Escape" && !$("a-pop").hidden) { e.preventDefault(); setPageMenu(false); $("a-menu").focus(); }
    else if (state.view === "accounts" && e.key === "Escape" && state.acc) { e.preventDefault(); go("company", state.acc.orgnr); }
    else if (!map && state.view !== "accounts" && (e.key === "k" || e.key === "K")) { e.preventDefault(); setChat(true); $("chat-in").focus(); }
  });

  // ---------- sidebar ----------

  function renderNav() {
    document.querySelectorAll(".nav [data-nav]").forEach((b) => {
      const v = b.dataset.nav;
      const on = v === state.view || (v === "collections" && state.view === "collection") || (v === "company" && (state.view === "home" || state.view === "structure"));
      if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    $("nav-company-sub").textContent = state.data ? state.data.company.name : "";
    $("nav-fav-count").textContent = state.favs.length || "";
    $("nav-coll-count").textContent = state.colls.length || "";
    $("side-colls").innerHTML = state.colls.map((c) =>
      `<li><button type="button" class="nav-btn" data-coll="${c.id}"${state.view === "collection" && state.collId === c.id ? ' aria-current="page"' : ""}>
        <span class="label">${esc(c.name)}</span><span class="count">${c.count}</span></button></li>`).join("");
    const cur = state.view === "company" ? state.orgnr : null;
    const recent = state.recent.slice(0, 6);
    $("side-recent").innerHTML = recent.map((r) =>
      `<li><button type="button" class="nav-btn" data-open="${esc(r.orgnr)}"${r.orgnr === cur ? ' aria-current="page"' : ""}><span class="label">${esc(r.name)}</span></button>
        <button type="button" class="side-x" data-unrecent="${esc(r.orgnr)}" aria-label="Fjern ${esc(r.name)} fra nylig sett" title="Fjern fra nylig sett">×</button></li>`).join("");
    $("side-recent-empty").hidden = recent.length > 0;
  }

  // × on a recent company: gone at once, back with a message if the server says no.
  $("side-recent").addEventListener("click", async (e) => {
    const x = e.target.closest("[data-unrecent]");
    if (!x) return;
    const orgnr = x.dataset.unrecent, before = state.recent;
    state.recent = state.recent.filter((r) => r.orgnr !== orgnr);
    state.unrecent.add(orgnr);
    renderNav();
    if (state.view === "home") renderHome();
    try {
      await api("DELETE", `/api/recent/${orgnr}`);
    } catch (err) {
      state.recent = before;
      state.unrecent.delete(orgnr);
      renderNav();
      if (state.view === "home") renderHome();
      setStatus(err.message || "Kunne ikke fjerne selskapet fra nylig sett.", true);
    }
  });

  async function refreshSide() {
    const [favs, colls, recent] = await Promise.all([
      api("GET", "/api/favourites").catch(() => null),
      api("GET", "/api/collections").catch(() => null),
      api("GET", "/api/recent").catch(() => null),
    ]);
    if (favs) state.favs = favs.items;
    if (colls) state.colls = colls.items;
    if (recent) state.recent = recent.items.filter((r) => !state.unrecent.has(r.orgnr));
    renderNav();
    if (state.view === "home") renderHome();
  }

  async function newCollection(name, orgnrs) {
    const res = await api("POST", "/api/collections", { name, orgnrs: orgnrs || [] });
    await refreshSide();
    return res;
  }

  async function createAndOpenCollection() {
    try {
      const res = await newCollection("Ny samling");
      go("collection", res.id);
      setTimeout(() => { $("coll-name").focus(); $("coll-name").select(); }, 150);
    } catch (e) { setStatus(e.message, true); }
  }
  $("side-new-coll").addEventListener("click", createAndOpenCollection);
  $("new-coll").addEventListener("click", createAndOpenCollection);

  // ---------- search (shared combobox) ----------

  /** Wire a search input + listbox. `onPick(hit)` gets a company, or with `people` also a person
   * from the shareholder register (`kind: "person"`); `exclude(orgnr)` hides companies. One row is
   * active at a time, moved by the arrow keys or the mouse, and Enter opens it. Hits are ordered by
   * search.js: the group whose best hit matches the search best comes first. */
  function combobox({ input, dropdown, list, spinner, onPick, exclude, tag, people = false }) {
    let results = [];
    let active = -1;
    let seq = 0;
    let timer = null;
    let pointer = null; // last mouse position over the list: only a real move takes the selection

    function close() {
      dropdown.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }

    function companyRow(c, i) {
      const meta = [fmtOrgnr(c.orgnr), c.form_code, c.place].filter(Boolean).join(" · ");
      return `<li role="option" id="${list.id}-${i}" class="opt" aria-selected="${i === 0}" data-i="${i}">
        <span><strong>${esc(c.name)}</strong><small>${esc(meta)}</small></span>
        <span class="tags">${badges(c)}${tag ? tag(c) : ""}</span></li>`;
    }
    function personRow(p, i) {
      const meta = [`f. ${p.birth_year}`, holderPlace(p)].filter(Boolean).join(" · ");
      const held = p.current ? `aksjer i ${plural(p.companies, "selskap", "selskaper")}` : `aksjer til ${p.last_year}`;
      return `<li role="option" id="${list.id}-${i}" class="opt" aria-selected="${i === 0}" data-i="${i}">
        <span><strong>${esc(p.name)}</strong><small>${esc(meta)}</small></span>
        <span class="tags"><span>${esc(held)}</span></span></li>`;
    }

    function allRow(h, i) {
      const n = `${h.capped ? "Over" : "Alle"} ${fmtInt(h.total)} personer`;
      return `<li role="option" id="${list.id}-${i}" class="opt" aria-selected="${i === 0}" data-i="${i}">
        <span><strong>${n} →</strong><small>med «${esc(h.q)}» i navnet</small></span></li>`;
    }

    function render(q, groups, peopleReady) {
      results = groups.flatMap((g) => g.hits);
      active = results.length ? 0 : -1;
      pointer = null;
      const rows = [];
      let i = 0;
      for (const g of groups) {
        // People always say where they're from; companies only when people are in the list too.
        if (g.kind === "person") rows.push('<li class="opt-head" role="presentation">Personer i aksjonærregisteret</li>');
        else if (groups.length > 1) rows.push('<li class="opt-head" role="presentation">Selskaper</li>');
        for (const h of g.hits) rows.push(h.kind === "person" ? personRow(h, i++) : h.kind === "people-all" ? allRow(h, i++) : companyRow(h, i++));
        if (g.more) rows.push('<li class="opt-note" role="presentation">Flere selskaper: skriv mer av navnet.</li>');
      }
      if (people && peopleReady === false) rows.push('<li class="opt-head" role="presentation">Personsøket gjøres klart. Prøv igjen om et par minutter.</li>');
      list.innerHTML = rows.length ? rows.join("")
        : `<li class="opt" aria-disabled="true"><span class="muted">Ingen treff på «${esc(q)}».</span></li>`;
      dropdown.hidden = false;
      input.setAttribute("aria-expanded", "true");
      if (active >= 0) input.setAttribute("aria-activedescendant", `${list.id}-0`);
      else input.removeAttribute("aria-activedescendant");
    }

    function highlight(i, scroll = true) {
      const items = list.querySelectorAll("[data-i]");
      if (!items.length) return;
      active = (i + items.length) % items.length;
      items.forEach((it, j) => it.setAttribute("aria-selected", String(j === active)));
      if (scroll) items[active].scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", `${list.id}-${active}`);
    }

    async function run(q) {
      const my = ++seq;
      if (spinner) spinner.hidden = false;
      try {
        const body = await api("GET", `/api/search?q=${encodeURIComponent(q)}${people ? "&people=true" : ""}`);
        if (my !== seq || input.value.trim() !== q) return;
        const companies = body.results.filter((c) => !exclude || !exclude(c.orgnr));
        render(q, searchGroups(companies, body.people || [], q, { total: body.people_total, capped: body.people_capped }), body.people_ready);
      } catch (e) {
        if (my === seq) { close(); setStatus(e.message || "Søket feilet.", true); }
      } finally {
        if (my === seq && spinner) spinner.hidden = true;
      }
    }

    function pick(hit) {
      close();
      onPick(hit);
    }

    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) { seq++; if (spinner) spinner.hidden = true; close(); return; }
      timer = setTimeout(() => run(q), 250);
    });
    input.addEventListener("keydown", (e) => {
      const open = !dropdown.hidden;
      if (e.key === "ArrowDown" && open) { e.preventDefault(); highlight(active + 1); }
      else if (e.key === "ArrowUp" && open) { e.preventDefault(); highlight(active - 1); }
      else if (e.key === "Escape") { close(); input.value = ""; input.blur(); }
      else if (e.key === "Enter") {
        e.preventDefault();
        const digits = input.value.replace(/\s+/g, "");
        if (open && active >= 0 && results[active]) pick(results[active]);
        else if (/^\d{9}$/.test(digits)) { close(); onPick({ kind: "company", orgnr: digits, name: digits }); }
      }
    });
    dropdown.addEventListener("mousedown", (e) => e.preventDefault()); // keep focus in input
    // The row under the mouse becomes the active one, so there is only ever one marked row and
    // Enter opens it. A list that scrolls under a still mouse doesn't move the selection.
    list.addEventListener("pointermove", (e) => {
      if (pointer && pointer[0] === e.clientX && pointer[1] === e.clientY) return;
      pointer = [e.clientX, e.clientY];
      const li = e.target.closest("[data-i]");
      if (li && Number(li.dataset.i) !== active) highlight(Number(li.dataset.i), false);
    });
    list.addEventListener("click", (e) => {
      const li = e.target.closest("[data-i]");
      if (li) pick(results[Number(li.dataset.i)]);
    });
    input.addEventListener("blur", () => setTimeout(close, 120));
    input.addEventListener("focus", () => { if (results.length && input.value.trim().length >= 2) dropdown.hidden = false; });
    return { close };
  }

  combobox({
    input: $("search-input"), dropdown: $("search-dropdown"), list: $("search-results"), spinner: $("search-spinner"),
    people: true,
    tag: (c) => (state.favs.some((f) => f.orgnr === c.orgnr) ? '<span>Favoritt</span>' : ""),
    onPick: (hit) => {
      $("search-input").value = "";
      $("search-input").blur();
      if (hit.kind === "person") go("person", { name: hit.name, born: hit.birth_year });
      else if (hit.kind === "people-all") go("people", { q: hit.q });
      else go("company", hit.orgnr);
    },
  });

  // ---------- home ----------

  function homeList(items) {
    return items.map((r) => `<li><button type="button" data-open="${esc(r.orgnr)}"><span>${esc(r.name)}</span><span class="muted num">${fmtOrgnr(r.orgnr)}</span></button></li>`).join("");
  }
  function renderHome() {
    $("home-favs-block").hidden = !state.favs.length;
    $("home-favs").innerHTML = homeList(state.favs);
    $("home-recent-block").hidden = !state.recent.length;
    $("home-recent").innerHTML = homeList(state.recent.slice(0, 8));
  }

  // ---------- company ----------

  async function openCompany(orgnr) {
    showView("company");
    stopSubsPoll();
    state.orgnr = orgnr;
    $("company").hidden = !(state.data && state.data.company.orgnr === orgnr);
    $("company-loading").hidden = false;
    try {
      const data = await api("GET", `/api/company/${orgnr}`);
      if (state.orgnr !== orgnr) return;
      state.data = data;
      // The server recorded this view; show it on top of Nylig sett now, not after refreshSide.
      state.unrecent.delete(orgnr);
      state.recent = recentFirst(state.recent, data.company);
      state.figYear = null;
      state.figScope = "selskap";
      state.pick = [];
      state.pickOpen = false;
      state.anchor = null;
      $("doc-q").value = "";
      state.subsQuery = "";
      state.subsAll = false;
      state.reg = null;
      state.accRows = data.accounts || [];
      state.readFig = null;
      state.figOff = 0;
      renderCompany();
      pollAccounts();
      loadReadFigures(orgnr);
      refreshSide();
      loadRegister(orgnr);
      loadNotices(orgnr);
    } catch (e) {
      if (state.orgnr !== orgnr) return;
      $("company").hidden = true;
      setStatus(e.message || "Kunne ikke hente selskapet.", true);
    } finally {
      if (state.orgnr === orgnr) $("company-loading").hidden = true;
    }
  }

  function renderCompany() {
    const d = state.data;
    const c = d.company;
    document.title = `${c.name} · Årsregnskap`;
    $("company").hidden = false;
    $("c-name").textContent = c.name;
    $("c-badges").innerHTML = badges(c);
    const facts = [`Org.nr. <span class="num">${fmtOrgnr(c.orgnr)}</span>`];
    if (c.form_desc || c.form_code) facts.push(esc(c.form_desc || c.form_code));
    if (c.place) facts.push(esc(c.place));
    if (c.industry) facts.push(esc(c.industry));
    if (c.deleted_date) facts.push(`slettet ${esc(c.deleted_date)}`);
    if (c.parent_orgnr) facts.push(`hovedenhet <button type="button" class="lk" data-open="${esc(c.parent_orgnr)}">${fmtOrgnr(c.parent_orgnr)}</button>`);
    facts.push(`<a href="https://virksomhet.brreg.no/nb/oppslag/enheter/${esc(c.orgnr)}" target="_blank" rel="noopener">Se hos Brreg ↗</a>`);
    $("c-facts").innerHTML = facts.join(" · ");
    renderFavBtn();
    renderCollPop();
    renderDocs();
    renderFigures();
    renderRoles();
    renderOwnership();
    renderNav();
    renderChatContext();
  }

  function renderFavBtn() {
    const on = !!state.data.favourite;
    $("fav-btn").setAttribute("aria-pressed", String(on));
    $("fav-btn").querySelector(".sq").setAttribute("aria-pressed", String(on));
    $("fav-label").textContent = on ? "Favoritt" : "Legg til favoritt";
    const n = (state.data.collections || []).length;
    $("coll-btn-label").textContent = n ? `Samlinger (${n})` : "Legg i samling";
  }

  async function toggleFav() {
    const d = state.data;
    if (!d) return;
    const on = !d.favourite;
    d.favourite = on;
    renderFavBtn();
    try {
      await api(on ? "PUT" : "DELETE", `/api/favourites/${d.company.orgnr}`);
      refreshSide();
    } catch (e) {
      d.favourite = !on;
      renderFavBtn();
      setStatus(e.message, true);
    }
  }
  $("fav-btn").addEventListener("click", toggleFav);

  // Years + downloads
  function span(sel) {
    if (sel.length === 1) return String(sel[0]);
    const ys = state.data.years;
    const contiguous = ys.indexOf(sel[sel.length - 1]) - ys.indexOf(sel[0]) === sel.length - 1;
    return contiguous ? `${sel[0]}–${sel[sel.length - 1]}` : sel.join(", ");
  }

  function renderDocs() {
    const d = state.data;
    const ys = d.years;
    const none = ys.length === 0;
    $("docs").hidden = none;
    $("dl-btn").hidden = none;
    setDlMenu(false);
    setMoreMenu(false);
    $("no-years").hidden = !none;
    if (none) {
      const c = d.company;
      $("no-years").innerHTML = c.kind === "underenhet" && c.parent_orgnr
        ? `Dette er en underenhet. Årsregnskapet ligger på hovedenheten: <button type="button" class="lk" data-open="${esc(c.parent_orgnr)}">${fmtOrgnr(c.parent_orgnr)}</button>.`
        : "Brreg har ingen kopier av årsregnskap for dette selskapet. Enkeltpersonforetak og mange foreninger leverer ikke årsregnskap.";
      return;
    }
    renderAccYears();
  }

  // ---------- Last ned ▾: every file for the company in one menu ----------

  function dlItem(label, years, fmt) {
    return `<button type="button" class="pop-it" data-dl="${years.join(",")}"${state.jobRunning ? " disabled" : ""}>`
      + `<span>${label} <span class="muted">· ${span(years)}</span></span><span class="fmt">${fmt}</span></button>`;
  }

  function pickLabel() {
    const n = state.pick.length;
    return n === 0 ? "Velg år" : n === 1 ? `Last ned ${state.pick[0]} (PDF)` : `Last ned ${n} år (ZIP)`;
  }

  function renderDlMenu() {
    const ys = state.data.years;
    const last = (n) => ys.slice(-n);
    const rows = [dlItem("Siste år", last(1), "PDF")];
    if (ys.length > 3) rows.push(dlItem("Siste 3 år", last(3), "ZIP"));
    if (ys.length > 5) rows.push(dlItem("Siste 5 år", last(5), "ZIP"));
    if (ys.length > 1) rows.push(dlItem(`Alle ${ys.length} år`, ys, "ZIP"));
    const pick = ys.length > 2
      ? `<button type="button" class="pop-it" id="dl-pick-btn" aria-expanded="${state.pickOpen}"><span>Velg år …</span><span class="fmt">PDF/ZIP</span></button>
        <div class="dl-pick" ${state.pickOpen ? "" : "hidden"}>
          <div class="years" role="group" aria-label="Velg år">${ys.map((y) => `<button type="button" class="yr" data-year="${y}" aria-pressed="${state.pick.includes(y)}">${y}</button>`).join("")}</div>
          <p class="hint">Shift-klikk velger et spenn.</p>
          <button type="button" class="primary sm" id="dl-pick-go"${!state.pick.length || state.jobRunning ? " disabled" : ""}>${pickLabel()}</button>
        </div>`
      : "";
    const xlsx = figureExport() ? `<h3>Tallene</h3><button type="button" class="pop-it" data-dl-xlsx><span>Nøkkeltall, alle år</span><span class="fmt">Excel</span></button>` : "";
    $("dl-pop").innerHTML = `<h3>Årsregnskap fra Brreg</h3>${rows.join("")}${pick}${xlsx}`;
  }

  function setDlMenu(open) {
    if (open) renderDlMenu();
    $("dl-pop").hidden = !open;
    $("dl-btn").setAttribute("aria-expanded", String(open));
  }

  $("dl-btn").addEventListener("click", () => setDlMenu($("dl-pop").hidden));
  $("dl-pop").addEventListener("click", (e) => {
    const t = e.target;
    const item = t.closest("[data-dl]");
    if (item) { setDlMenu(false); return downloadCompanyYears(item.dataset.dl.split(",").map(Number)); }
    if (t.closest("[data-dl-xlsx]")) { setDlMenu(false); return downloadFiguresXlsx(); }
    if (t.closest("#dl-pick-go")) { setDlMenu(false); return downloadCompanyYears(state.pick); }
    if (t.closest("#dl-pick-btn")) {
      state.pickOpen = !state.pickOpen;
      $("dl-pick-btn").setAttribute("aria-expanded", String(state.pickOpen));
      $("dl-pop").querySelector(".dl-pick").hidden = !state.pickOpen;
      return;
    }
    const b = t.closest("[data-year]");
    if (!b) return;
    const y = Number(b.dataset.year);
    const ys = state.data.years;
    if (e.shiftKey && state.anchor !== null) {
      const lo = Math.min(state.anchor, y);
      const hi = Math.max(state.anchor, y);
      state.pick = [...new Set([...state.pick, ...ys.filter((v) => v >= lo && v <= hi)])].sort((a, b2) => a - b2);
    } else {
      state.pick = state.pick.includes(y) ? state.pick.filter((v) => v !== y) : [...state.pick, y].sort((a, b2) => a - b2);
    }
    state.anchor = y;
    $("dl-pop").querySelectorAll("[data-year]").forEach((c) => c.setAttribute("aria-pressed", String(state.pick.includes(Number(c.dataset.year)))));
    $("dl-pick-go").textContent = pickLabel();
    $("dl-pick-go").disabled = !state.pick.length || state.jobRunning;
  });

  function downloadCompanyYears(years) {
    const c = state.data.company;
    if (!years.length) return;
    if (years.length === 1) return downloadSingle(c.orgnr, years[0]);
    startJob({ orgnrs: [c.orgnr], mode: "years", years, batch: false }, `${c.name}: ${years.length} årsregnskap`);
  }

  async function downloadSingle(orgnr, year) {
    setStatus(`Henter ${year} …`);
    try {
      const resp = await fetch(`/api/pdf/${orgnr}/${year}`);
      if (!resp.ok) throw new Error(await apiError(resp));
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const name = filenameFrom(resp, `${orgnr}_${year}.pdf`);
      triggerDownload(url, name);
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      setStatus(`Lastet ned ${name}`);
    } catch (e) {
      setStatus(e.message || "Nedlastingen feilet.", true);
    }
  }

  // ---------- reading årsregnskap (tall, noter og tekst) ----------

  const accBusy = (r) => r.status === "queued" || r.status === "running";
  const accByYear = () => new Map(state.accRows.map((r) => [r.year, r]));

  /** What a running read is doing: short for a year button ("s. 12/29"), long for the reader ("leser side 12 av 29"). */
  function readProgress(r, short) {
    if (r.status === "queued") return "i kø";
    if (r.phase === "leser" && r.pages_total) {
      const page = Math.min(r.pages_done + 1, r.pages_total);
      return short ? `s. ${page}/${r.pages_total}` : `leser side ${page} av ${r.pages_total}`;
    }
    if (r.phase === "kontrollerer") return short ? "kontrollerer" : "kontrollerer tallene";
    return "henter PDF";
  }

  /** A year in the row: read opens the reader, being read shows progress, failed retries. "" for an unread year: it
   * sits behind "Les inn flere år". */
  function yearButton(y) {
    const r = accByYear().get(y);
    if (!r) return "";
    if (r.status === "ok") {
      return `<button type="button" class="doc-yr" data-reader="${y}" title="Åpne ${y} i leseren${r.pages_total ? ` · ${r.pages_total} sider` : ""}">${y}</button>`;
    }
    if (accBusy(r)) {
      return `<button type="button" class="doc-yr busy" aria-disabled="true" title="${y} leses inn nå">${y} <small>${readProgress(r, true)}</small></button>`;
    }
    if (r.status === "failed") {
      return `<button type="button" class="doc-yr err" data-read="${y}" title="${esc(failReason(r.error))}">${y} <small>Prøv igjen</small></button>`;
    }
    return "";
  }

  const readYearsOk = () => state.accRows.filter((r) => r.status === "ok").map((r) => r.year).sort((a, b) => a - b);

  function renderAccYears() {
    if (!state.data || !state.data.years.length) return;
    $("doc-years").innerHTML = state.data.years.slice().reverse().map(yearButton).join("");
    const unread = unreadYears(state.data.years, [], state.accRows);
    $("doc-more").hidden = !unread.length;
    if (!unread.length) setMoreMenu(false);
    $("doc-more-label").textContent = $("doc-years").childElementCount ? "Les inn flere år" : "Les inn";
    const read = readYearsOk();
    $("doc-search").hidden = !read.length;
    if (read.length) $("doc-q").placeholder = `Søk i ${read.length === 1 ? "årsregnskapet" : "årsregnskapene"} ${span(read)}: tekst og tall`;
    const failed = state.accRows.filter((r) => r.status === "failed");
    $("doc-note").hidden = read.length > 0 && !failed.length;
    $("doc-note").innerHTML = failed.length
      ? failed.map((r) => `<span class="err-t">${r.year}: ${esc(failReason(r.error))}</span>`).join(" · ")
      : "Les inn et år for å åpne det i leseren og søke i tall, noter og tekst.";
  }

  function pollAccounts() {
    clearTimeout(state.accTimer);
    const orgnr = state.view === "accounts" && state.acc ? state.acc.orgnr : state.orgnr;
    if (!state.accRows.some(accBusy) || !["company", "accounts"].includes(state.view)) return;
    state.accTimer = setTimeout(async () => {
      try {
        const res = await api("GET", `/api/company/${orgnr}/accounts`);
        const wasBusy = new Set(state.accRows.filter(accBusy).map((r) => r.year));
        state.accRows = res.years;
        if (state.view === "company" && state.orgnr === orgnr) {
          renderAccYears();
          renderFigures();
          if (res.years.some((r) => wasBusy.has(r.year) && r.status === "ok")) loadReadFigures(orgnr);
        }
        if (state.view === "accounts" && state.acc && state.acc.orgnr === orgnr) {
          renderAccYearSwitch();
          const done = res.years.find((r) => wasBusy.has(r.year) && r.year === state.acc.year && r.status === "ok");
          if (done && !state.acc.d) loadAccount(orgnr, state.acc.year);
        }
      } catch (e) { /* try again on the next tick */ }
      pollAccounts();
    }, ACC_POLL_MS);
  }

  async function readYears(years, btn) {
    if (btn) btn.disabled = true;
    try {
      state.accRows = (await api("POST", `/api/company/${state.data.company.orgnr}/accounts`, { years })).years;
      renderAccYears();
      renderFigures();
      pollAccounts();
    } catch (e) {
      if (btn) btn.disabled = false;
      setStatus(e.message || "Kunne ikke starte innlesingen.", true);
    }
  }

  /** Les inn flere år ▾: one chip per unread year, newest first; a click starts that read. */
  function setMoreMenu(open) {
    const pop = $("doc-more-pop");
    if (open) $("doc-more-years").innerHTML = unreadYears(state.data.years, [], state.accRows).map((y) => `<button type="button" class="yr" data-read="${y}">${y}</button>`).join("");
    pop.hidden = !open;
    $("doc-more-btn").setAttribute("aria-expanded", String(open));
    // Far right in a long row the menu would stick out past the column: open it leftwards
    pop.classList.remove("flip");
    if (open) pop.classList.toggle("flip", pop.getBoundingClientRect().right > $("docs").getBoundingClientRect().right);
  }

  $("docs").addEventListener("click", (e) => {
    if (e.target.closest("#doc-more-btn")) return setMoreMenu($("doc-more-pop").hidden);
    const open = e.target.closest("[data-reader]");
    if (open) return go("accounts", { orgnr: state.data.company.orgnr, year: Number(open.dataset.reader) });
    const rd = e.target.closest("[data-read]");
    if (!rd) return;
    if (!$("doc-more-pop").hidden) { setMoreMenu(false); $("doc-more-btn").focus(); }
    readYears([Number(rd.dataset.read)], rd);
  });

  // Search box: the reader's search over every read year, opened on the newest one.
  const DOC_SUGG = ["nærstående", "lån til daglig leder", "utbytte", "konsernbidrag", "husleie", "pantstillelser"];
  function setDocSugg(on) {
    $("doc-sugg").hidden = !on;
    if (on) $("doc-sugg").innerHTML = `<p>For eksempel</p>${DOC_SUGG.map((q) => `<button type="button" data-q="${q}">${q}</button>`).join("")}`;
  }
  function searchAccounts(q) {
    const read = readYearsOk();
    if (!read.length || q.trim().length < 2) return;
    go("accounts", { orgnr: state.data.company.orgnr, year: read[read.length - 1], q: q.trim() });
  }
  $("doc-search").addEventListener("submit", (e) => { e.preventDefault(); setDocSugg(false); searchAccounts($("doc-q").value); });
  $("doc-q").addEventListener("focus", () => setDocSugg(!$("doc-q").value));
  $("doc-q").addEventListener("input", () => setDocSugg(!$("doc-q").value));
  $("doc-sugg").addEventListener("click", (e) => { const b = e.target.closest("[data-q]"); if (b) { setDocSugg(false); searchAccounts(b.dataset.q); } });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#doc-search")) setDocSugg(false);
    if (!$("dl-pop").hidden && !e.target.closest("#dl-pop, #dl-btn")) setDlMenu(false);
    if (!$("doc-more-pop").hidden && !e.target.closest("#doc-more")) setMoreMenu(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("dl-pop").hidden) { setDlMenu(false); $("dl-btn").focus(); }
    else if (!$("doc-more-pop").hidden) { setMoreMenu(false); $("doc-more-btn").focus(); }
    else if (!$("doc-sugg").hidden) setDocSugg(false);
  });

  // Reader view: contents | extracted section | scanned page.
  async function openAccounts(arg) {
    showView("accounts");
    state.acc = { orgnr: arg.orgnr, year: arg.year, d: null, secs: [], cur: null, page: 1, hl: null, goto: arg.goto || null, search: null };
    $("a-q").value = arg.q || "";
    $("a-back").dataset.open = arg.orgnr;
    $("a-title").textContent = `Årsregnskap ${arg.year}`;
    $("a-grid").hidden = true;
    $("a-msg").innerHTML = `<p class="working"><span class="spinner"></span> Henter …</p>`;
    try {
      if (!(state.data && state.data.company.orgnr === arg.orgnr)) {
        state.orgnr = arg.orgnr;
        state.data = await api("GET", `/api/company/${arg.orgnr}`);
      }
      state.accRows = (await api("GET", `/api/company/${arg.orgnr}/accounts`)).years;
    } catch (e) {
      $("a-msg").innerHTML = `<p class="note">${esc(e.message || "Kunne ikke hente selskapet.")}</p>`;
      return;
    }
    if (!state.acc || state.acc.orgnr !== arg.orgnr) return;
    const name = state.data.company.name;
    $("a-back").textContent = `← ${name}`;
    document.title = `${name} · årsregnskap ${arg.year}`;
    renderAccYearSwitch();
    pollAccounts();
    loadAccount(arg.orgnr, arg.year);
  }

  function renderAccYearSwitch() {
    const ok = state.accRows.filter((r) => r.status === "ok");
    $("a-years").innerHTML = ok.map((r) => `<button type="button" data-a-year="${r.year}" aria-pressed="${r.year === state.acc.year}">${r.year}</button>`).join("");
    $("a-years").hidden = ok.length < 2;
    const busy = state.accRows.filter(accBusy);
    $("a-meta").innerHTML = busy.map((r) => `<span><b>${r.year}</b>: ${readProgress(r)}</span>`).join(" · ");
  }

  async function loadAccount(orgnr, year) {
    const row = state.accRows.find((r) => r.year === year);
    if (!row || row.status !== "ok") {
      $("a-grid").hidden = true;
      $("a-msg").innerHTML = row && accBusy(row)
        ? `<p class="working"><span class="spinner"></span> ${year} leses inn nå. Siden oppdateres når den er ferdig.</p>`
        : `<p class="note">${year} er ikke lest inn ennå. <button type="button" class="lk" data-open="${orgnr}">Les det inn fra selskapssiden</button>.</p>`;
      return;
    }
    try {
      const d = await api("GET", `/api/company/${orgnr}/accounts/${year}`);
      const a = state.acc;
      if (!a || a.orgnr !== orgnr || a.year !== year) return;
      a.d = d;
      a.secs = sections(d);
      a.cur = d.statements.resultat ? "resultat" : (a.secs[0] || {}).id || "checks";
      a.page = (a.secs.find((s) => s.id === a.cur) || { pages: [1] }).pages[0] || 1;
      if (a.goto) {  // arrived from a search hit in another year
        const s = sectionForPage(a.secs, d, a.goto.page, a.goto.note);
        if (s) a.cur = s.id;
        a.page = a.goto.page;
      }
      $("a-msg").innerHTML = "";
      $("a-grid").hidden = false;
      $("a-pdf").href = `/api/company/${orgnr}/accounts/${year}/pdf`;
      renderAccount();
      // arrived from the company page's search box: show the hits
      if (!a.goto && $("a-q").value.trim().length >= 2) runSearch();
    } catch (e) {
      $("a-msg").innerHTML = `<p class="note">${esc(e.message || "Kunne ikke hente årsregnskapet.")}</p>`;
    }
  }

  function renderAccount() {
    const a = state.acc;
    if (a.search) { renderSearch(); return; }
    $("a-toc").innerHTML = tocHtml(a.d, a.secs, a.cur);
    $("a-main").innerHTML = mainHtml(a.d, a.secs, a.cur, a.hl) + `<p class="keys">Klikk en linje eller et avsnitt for å se siden. Teksten er skrevet av fra skannet.</p>`;
    renderScan();
  }

  // Scan header: ‹ ›, page number field, "what's on it ▾" opening a list of every page, ‹ › between pages to check.
  function renderScan() {
    const a = state.acc;
    const n = a.d.doc.n_pages;
    if (document.activeElement !== $("a-pn")) $("a-pn").value = a.page;
    $("a-pt").textContent = n;
    $("a-pk").textContent = pageTitle(a.d, a.secs, a.page);
    $("a-prev").disabled = a.page <= 1;
    $("a-next").disabled = a.page >= n;
    const st = checkStep(a.d, a.page);
    $("a-chk").hidden = !st.total;
    $("a-chk-n").textContent = st.at ? `Å sjekke ${st.at}/${st.total}` : "Å sjekke";
    $("a-chk-n").className = st.at ? "flagnote" : "muted";
    $("a-chk").title = `Sider med noe å sjekke: ${st.total}`;
    $("a-chk-prev").disabled = st.prev === null;
    $("a-chk-next").disabled = st.next === null;
    $("a-img").src = `/api/company/${a.orgnr}/accounts/${a.year}/pages/${a.page}`;
    $("a-pdf").href = `/api/company/${a.orgnr}/accounts/${a.year}/pdf#page=${a.page}`;
    if (!$("a-pop").hidden) renderPageList();
  }

  function renderPageList() {
    const a = state.acc;
    $("a-plist").innerHTML = pageListHtml(a.d, a.secs, a.page, $("a-pfilter").value);
  }

  function setPageMenu(open) {
    $("a-pop").hidden = !open;
    $("a-menu").setAttribute("aria-expanded", String(open));
    if (!open) return;
    $("a-pfilter").value = "";
    renderPageList();
    $("a-pfilter").focus();
    const cur = $("a-plist").querySelector('[aria-current="true"]');
    if (cur) cur.scrollIntoView({ block: "center" });
  }

  function renderSearch() {
    const a = state.acc;
    $("a-toc").innerHTML = tocHtml(a.d, a.secs, null);
    $("a-main").innerHTML = searchHtml(a.search);
    renderScan();
  }

  let searchSeq = 0;
  async function runSearch() {
    const a = state.acc;
    const q = $("a-q").value.trim();
    const seq = ++searchSeq;
    if (q.length < 2) { if (a.search) { a.search = null; renderAccount(); } return; }
    try {
      const res = await api("GET", `/api/company/${a.orgnr}/accounts/search?q=${encodeURIComponent(q)}`);
      if (seq !== searchSeq || state.acc !== a || !a.d) return;
      a.search = res;
      renderSearch();
    } catch (e) {
      setStatus(e.message || "Søket feilet.", true);
    }
  }
  let searchTimer = null;
  $("a-q").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 250); });

  function openHit(el) {
    const a = state.acc;
    const year = Number(el.dataset.hitYear), page = Number(el.dataset.hitPage), note = el.dataset.hitNote || null;
    if (year !== a.year) { go("accounts", { orgnr: a.orgnr, year, goto: { page, note }, q: $("a-q").value }); return; }
    a.search = null;
    const s = sectionForPage(a.secs, a.d, page, note);
    if (s) a.cur = s.id;
    a.page = page;
    a.hl = null;
    renderAccount();
  }

  function goSection(id) {
    const a = state.acc;
    a.search = null;
    a.cur = id;
    a.hl = null;
    const s = a.secs.find((x) => x.id === id);
    if (id === "checks" && a.d.checks.length) a.page = a.d.checks[0].page || a.page;
    else if (s && s.pages.length) a.page = s.pages[0];
    renderAccount();
  }

  function showPage(n) {
    const a = state.acc;
    a.page = Math.max(1, Math.min(a.d.doc.n_pages, n));
    renderScan();
  }

  $("a-toc").addEventListener("click", (e) => { const b = e.target.closest("[data-sec]"); if (b) goSection(b.dataset.sec); });
  $("a-toc").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const ids = ["checks", ...state.acc.secs.map((s) => s.id)];
    const i = ids.indexOf(state.acc.cur);
    goSection(ids[Math.max(0, Math.min(ids.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))]);
    $("a-toc").querySelector('[aria-current="true"]').focus();
  });
  $("a-main").addEventListener("click", (e) => {
    const a = state.acc;
    const hit = e.target.closest("[data-hit-year]");
    if (hit) { openHit(hit); return; }
    const note = e.target.closest("[data-note]");
    if (note) { const id = noteSection(a.d, note.dataset.note); if (id) goSection(id); return; }
    const el = e.target.closest("[data-page]");
    if (!el || !el.dataset.page) return;
    a.hl = el.dataset.row !== undefined ? Number(el.dataset.row) : null;
    a.page = Number(el.dataset.page);
    if (el.dataset.row !== undefined) $("a-main").innerHTML = mainHtml(a.d, a.secs, a.cur, a.hl) + `<p class="keys">Klikk en linje eller et avsnitt for å se siden. Teksten er skrevet av fra skannet.</p>`;
    renderScan();
  });
  $("a-prev").addEventListener("click", () => showPage(state.acc.page - 1));
  $("a-next").addEventListener("click", () => showPage(state.acc.page + 1));
  $("a-pn").addEventListener("focus", () => $("a-pn").select());
  $("a-pn").addEventListener("blur", () => { $("a-pn").value = state.acc.page; });
  $("a-pn").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { $("a-pn").blur(); return; }
    if (e.key !== "Enter") return;
    const n = parseInt($("a-pn").value, 10);
    if (n >= 1 && n <= state.acc.d.doc.n_pages) { showPage(n); $("a-pn").select(); } else $("a-pn").select();
  });
  $("a-menu").addEventListener("click", () => setPageMenu($("a-pop").hidden));
  $("a-pfilter").addEventListener("input", renderPageList);
  $("a-pfilter").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); setPageMenu(false); $("a-menu").focus(); return; }
    if (e.key !== "Enter") return;
    const first = $("a-plist").querySelector("[data-page]");
    if (first) { setPageMenu(false); showPage(Number(first.dataset.page)); }
  });
  $("a-plist").addEventListener("click", (e) => {
    const b = e.target.closest("[data-page]");
    if (b) { setPageMenu(false); showPage(Number(b.dataset.page)); }
  });
  document.addEventListener("click", (e) => {
    if (!$("a-pop").hidden && !e.target.closest("#a-pop, #a-menu")) setPageMenu(false);
  });
  $("a-chk-prev").addEventListener("click", () => { const p = checkStep(state.acc.d, state.acc.page).prev; if (p) showPage(p); });
  $("a-chk-next").addEventListener("click", () => { const p = checkStep(state.acc.d, state.acc.page).next; if (p) showPage(p); });
  $("a-years").addEventListener("click", (e) => {
    const b = e.target.closest("[data-a-year]");
    if (b) go("accounts", { orgnr: state.acc.orgnr, year: Number(b.dataset.aYear) });
  });
  $("a-more").addEventListener("click", () => {
    go("company", state.acc.orgnr);
  });

  // Figures: one year (two tables) or all years as one table, plus small charts per key figure.
  const cell = (get, sign, f) => {
    const v = get(f);
    if (sign === 0) return pct(v);
    return v === null || v === undefined ? "–" : k(sign < 0 ? -v : v);
  };
  const yearOf = (f) => `${ddmm(f.to_date)}${f.to_date ? f.to_date.slice(0, 4) : ""}`;

  function figTable(caption, rows, kf, history) {
    const body = visibleRows(rows, history).map(([label, get, kind, sign]) => {
      const text = label === "Skattekostnad" && (kf.skattekostnad === null || kf.skattekostnad === undefined)
        ? 'Skattekostnad <span class="muted">(avledet)</span>'
        : label === "Egenkapital" && kf.innskutt_egenkapital !== null && kf.innskutt_egenkapital !== undefined && kf.opptjent_egenkapital !== null
          ? `Egenkapital <span class="muted">(innskutt ${k(kf.innskutt_egenkapital)}, opptjent ${k(kf.opptjent_egenkapital)})</span>`
          : label;
      return `<tr class="${kind}"><td>${text}</td><td>${cell(get, sign, kf)}</td></tr>`;
    }).join("");
    return `<table class="fig"><thead><tr><th scope="col">${caption}</th><th scope="col"><span class="vh">Beløp</span></th></tr></thead><tbody>${body}</tbody></table>`;
  }

  // Years read from the PDF join Brreg's in the all-years table: one narrow column offers the next older year,
  // each figure links to its sources (GET …/accounts/figures).
  const RANGES = [[3, "Siste 3"], [5, "Siste 5"], [10, "Siste 10"]];

  function figHistories() {
    const rf = state.readFig || {};
    const ownB = state.data.key_figures_history || (state.data.key_figures ? [state.data.key_figures] : []);
    const groupB = state.data.key_figures_konsern_history || [];
    return {
      own: mergeHistory(ownB, (rf.selskap || {}).years), group: mergeHistory(groupB, (rf.konsern || {}).years),
      brregYears: [...new Set([...ownB, ...groupB].map((f) => f.year))],
    };
  }
  const figChecks = (f, field) => {
    if (f.pdf) return (f.checks || {})[field];
    const b = ((state.readFig || {})[state.figScope] || {}).brreg || {};
    return (b[f.year] || {})[field];
  };

  async function loadReadFigures(orgnr) {
    if (!state.accRows.some((r) => r.status === "ok")) { state.readFig = null; return; }
    try {
      const rf = await api("GET", `/api/company/${orgnr}/accounts/figures`);
      if (state.orgnr !== orgnr) return;
      state.readFig = rf;
      if (state.view === "company") renderFigures();
    } catch (e) { /* the table keeps Brreg's years */ }
  }

  /** Columns oldest → newest: data years in view, years being read, failed years and the next year to read. */
  function figColumns(history, shown) {
    const rows = new Map(state.accRows.map((r) => [r.year, r]));
    const data = new Set(history.map((f) => f.year));
    const oldestShown = shown.length ? shown[shown.length - 1].year : Infinity;
    const all = shown.length === history.length;
    const cols = shown.map((f) => ({ kind: "data", year: f.year, f, row: rows.get(f.year) }));
    for (const r of state.accRows) {
      if (data.has(r.year) || !(all || r.year > oldestShown)) continue;
      if (busyRow(r)) cols.push({ kind: "busy", year: r.year, row: r });
      else if (r.status === "failed") cols.push({ kind: "failed", year: r.year, row: r });
    }
    const next = unreadYears(state.data.years, [...data], state.accRows)[0];
    if (next !== undefined && all) cols.push({ kind: "next", year: next });
    return cols.sort((a, b) => a.year - b.year);
  }

  function figHead(c) {
    if (c.kind === "next") {
      return `<th scope="col" class="fig-next">${c.year}<button type="button" class="fig-mini" data-fig-read="${c.year}" title="Hele årsregnskapet for ${c.year}: tall, noter og tekst">Les inn</button></th>`;
    }
    if (c.kind === "failed") {
      return `<th scope="col" class="fig-next">${c.year}<span class="src red" title="${esc(c.row.error || "")}">feilet</span><button type="button" class="fig-mini" data-fig-read="${c.year}" title="${esc(c.row.error || "")}">Prøv igjen</button></th>`;
    }
    const busy = c.row && busyRow(c.row);
    const st = busy ? null : yearStatus(c.f, ((state.readFig || {})[state.figScope] || {}).brreg?.[c.year], state.data.years, state.accRows);
    // A year shown from Brreg's data whose PDF isn't read: reading it checks Brreg's figures against the filing
    const offer = !busy && !c.f.pdf && state.data.years.includes(c.year) && !(c.row && c.row.status === "ok");
    const btn = offer ? ` <button type="button" class="fig-mini inline" data-fig-read="${c.year}" title="Hele årsregnskapet for ${c.year}: tall, noter og tekst. Brregs tall kontrolleres mot PDF-en.">Les inn</button>` : "";
    const sub = busy ? `<span class="src muted"><span class="spinner sm"></span> ${progressText(c.row)}</span>` : `<span class="src ${st.cls}">${st.text}${btn}</span>`;
    return `<th scope="col" data-col="${c.year}">${c.year}${sub}</th>`;
  }

  function figCell(c, label, get, sign) {
    if (c.kind === "next" || c.kind === "failed") return '<td class="fig-next"></td>';
    if (c.kind === "busy") return '<td class="pend">·</td>';
    const field = FIELD[label];
    if (notFound(c.f, field)) return '<td class="miss" title="Fant ikke linjen i PDF-en. Se skannet.">ikke funnet</td>';
    const ch = field && figChecks(c.f, field);
    if (!ch) return `<td>${cell(get, sign, c.f)}</td>`;
    const cls = ch.st === "restated" ? "amber" : ch.st === "check" ? "red" : "";
    return `<td class="src-cell ${cls}" tabindex="0" data-fy="${c.year}" data-ff="${field}" data-fl="${esc(label)}" data-fs="${sign}">${cell(get, sign, c.f)}</td>`;
  }

  /** Every year as a column, oldest → newest. */
  function allYearsTable(history, shown) {
    const cols = figColumns(history, shown);
    const head = cols.map(figHead).join("");
    const span = cols.length + 1;
    const fs = shown.map((c) => c);
    const section = (title, rows) => `<tr class="sec"><th scope="rowgroup" colspan="${span}"><span>${title}</span></th></tr>${
      visibleRows(rows, fs).map(([label, get, kind, sign]) => `<tr class="${kind}"><td>${label}</td>${
        cols.map((c) => figCell(c, label, get, sign)).join("")}</tr>`).join("")}`;
    return `<table class="fig multi"><thead><tr><th scope="col"><span class="vh">Post</span></th>${head}</tr></thead><tbody>${
      section("Resultat", RESULT_ROWS)}${section("Balanse 31.12.", [...BALANCE_ROWS, EK_ROW])}</tbody></table>`;
  }

  function renderFigures() {
    const { own, group, brregYears } = figHistories();
    if (!own.length && group.length) state.figScope = "konsern";
    const history = state.figScope === "konsern" ? group : own;
    if (!history.length) {
      closeFigPop();
      const next = unreadYears(state.data.years, [], state.accRows)[0];
      const busy = state.accRows.filter(busyRow);
      $("fig-title").textContent = "Nøkkeltall";
      $("fig-ctl").innerHTML = "";
      $("fig-acts").hidden = true;
      $("fig-meta").textContent = "Ikke tilgjengelig";
      $("figures").innerHTML = `<p class="note">Brreg har ikke nøkkeltall som data for dette selskapet${state.data.years.length ? ". Årsregnskapene finnes som PDF" : ""}. Banker, forsikring og enkelte andre selskaper mangler slike data.</p>${
        busy.length ? `<p class="note"><span class="spinner sm"></span> Leser ${busy.map((r) => `${r.year} (${progressText(r)})`).join(", ")}</p>`
        : next !== undefined ? `<p><button type="button" class="tool-btn" data-fig-read="${next}">Les inn ${next}</button></p>` : ""}`;
      return;
    }
    state.figView ??= storeGet("regnskap.figView", "year");
    state.figRange ??= storeGet("regnskap.figRange", "all");
    const many = history.length > 3;
    const multi = history.length > 1;
    const all = many || (multi && state.figView === "all");
    const kf = history.find((f) => f.year === state.figYear) || history[0];
    const range = many && state.figRange !== "all" && +state.figRange < history.length ? +state.figRange : history.length;
    state.figOff = Math.max(0, Math.min(state.figOff || 0, history.length - range));
    const shown = history.slice(state.figOff, state.figOff + range);
    const span = `${shown[shown.length - 1].year}–${shown[0].year}`;
    const unit = kf.currency && kf.currency !== "NOK" ? `1 000 ${kf.currency}` : "1 000 kr";
    const kind = !group.length ? "Regnskap" : state.figScope === "konsern" ? "Konsernregnskap" : "Selskapsregnskap";
    $("fig-title").textContent = `${kind} ${all ? (shown.length > 1 ? span : shown[0].year) : kf.year}`;
    const winding = (all ? shown : [kf]).some((f) => f.avviklingsregnskap);
    const note = flagNote(all ? figFlags(shown) : []);
    $("fig-meta").innerHTML = `<span>${all ? unit : `${unit} · ${ddmm(kf.from_date)}–${yearOf(kf)}`}${
      winding ? ' · <b class="avvikling" title="Selskapet er under avvikling">Avviklingsregnskap</b>' : ""}${
      all && shown.some((f) => f.pdf) ? " · eldre år lest fra årsregnskapene; klikk et tall for kildene" : ""}</span>${
      note ? `<button type="button" class="lk fig-flag ${note.cls}" data-fig-flag>${note.text}</button>` : ""}`;
    const scopeCtl = own.length && group.length
      ? `<div class="fig-seg" role="group" aria-label="Regnskap for">${[["selskap", "Selskap"], ["konsern", "Konsern"]].map(([v, l]) =>
        `<button type="button" data-fig-scope="${v}" aria-pressed="${state.figScope === v}">${l}</button>`).join("")}</div>`
      : "";
    const yearCtl = many
      ? `<div class="fig-seg" role="group" aria-label="Antall år">${RANGES.filter(([n]) => n < history.length).map(([n, l]) =>
        `<button type="button" data-fig-range="${n}" aria-pressed="${range === n}">${l}</button>`).join("")
      }<button type="button" data-fig-range="all" aria-pressed="${range === history.length}">Alle ${history.length}</button></div>`
      : multi
        ? `<div class="fig-seg" role="group" aria-label="Vis år">${history.slice().reverse().map((f) =>
          `<button type="button" data-fig-year="${f.year}" aria-pressed="${!all && f.year === kf.year}">${f.year}</button>`).join("")
        }<button type="button" data-fig-all aria-pressed="${all}" title="Alle årene i én tabell">${spanLabel(history)}</button></div>`
        : "";
    const nav = all ? `<span class="fig-nav" role="group" aria-label="Bla i årene"><button type="button" data-fig-step="-1" aria-label="Eldre år" title="Eldre år (←)">‹</button><button type="button" data-fig-step="1" aria-label="Nyere år" title="Nyere år (→)">›</button></span>` : "";
    const unread = unreadYears(state.data.years, history.map((f) => f.year), state.accRows).filter((y) => y < history[0].year);
    const readAll = unread.length > 1
      ? `<button type="button" class="tool-btn fig-all-btn" data-fig-read-all title="Hele årsregnskapet for ${unread[unread.length - 1]}–${unread[0]}: tall, noter og tekst, nyeste først">Les inn alle ${unread.length} eldre år</button>`
      : unread.length && !all ? `<button type="button" class="tool-btn fig-all-btn" data-fig-read="${unread[0]}" title="Hele årsregnskapet for ${unread[0]}: tall, noter og tekst">Les inn ${unread[0]}</button>` : "";
    $("fig-ctl").innerHTML = scopeCtl + yearCtl + nav + readAll;
    $("fig-acts").hidden = false;
    const meta = [];
    if (kf.revidert === true) meta.push("Revidert");
    if (kf.revidert === false) meta.push("Ikke revidert");
    if (kf.smaa_foretak === true) meta.push("regler for små foretak");
    else if (kf.regnskapsregler && /alminnelig/i.test(kf.regnskapsregler)) meta.push("alminnelige regler");
    if (kf.morselskap && state.figScope !== "konsern") meta.push("morselskap");
    if (kf.archived) meta.push("lagret tidligere; Brreg viser ikke lenger dette året");
    if (kf.pdf) meta.push("lest fra årsregnskapet");
    const ekPct = kf.ek_andel === null || kf.ek_andel === undefined ? null : Math.max(0, Math.min(1, kf.ek_andel)) * 100;
    if (!all) {
      closeFigPop();
      $("figures").innerHTML = `<div class="tables">${figTable("Resultat", RESULT_ROWS, kf, history)}${figTable(`Balanse ${yearOf(kf)}`, BALANCE_ROWS, kf, history)}</div>
        <div class="ek">${ekPct === null ? "" : `<b>EK-andel ${pct(kf.ek_andel)}</b><span class="bar" aria-hidden="true"><i style="width:${ekPct}%"></i></span>`}
        <span class="muted">${esc(meta.join(" · "))}</span></div>`;
      return;
    }
    // Re-rendered while reads progress: keep the table where the reader scrolled it, counted from the newest end
    const old = $("figures").querySelector(".fig-scroll");
    const fromRight = old ? old.scrollWidth - old.scrollLeft : null;
    $("figures").innerHTML = `<div class="fig-scroll sticky">${allYearsTable(history, shown)}</div>`;
    const sc = $("figures").querySelector(".fig-scroll");
    sc.scrollLeft = fromRight === null || state.figToLeft ? (state.figToLeft ? 0 : sc.scrollWidth) : sc.scrollWidth - fromRight;
    state.figToLeft = false;
    sc.addEventListener("scroll", figNavState, { passive: true });
    figNavState();
    const openTd = figPop && sc.querySelector(`td[data-fy="${figPop.dataset.y}"][data-ff="${figPop.dataset.f}"]`);
    if (openTd) openTd.classList.add("open"); else closeFigPop();
  }

  /** Arrows step a limited range one year; with every year shown they scroll the table one column. */
  function figNavState() {
    const sc = $("figures").querySelector(".fig-scroll");
    const nav = $("fig-ctl").querySelector(".fig-nav");
    if (!sc || !nav) return;
    const { own, group } = figHistories();
    const history = state.figScope === "konsern" ? group : own;
    const limited = state.figRange !== "all" && +state.figRange < history.length && history.length > 3;
    const older = limited ? state.figOff + +state.figRange < history.length : sc.scrollLeft > 1;
    const newer = limited ? state.figOff > 0 : sc.scrollLeft + sc.clientWidth < sc.scrollWidth - 1;
    nav.querySelector('[data-fig-step="-1"]').disabled = !older;
    nav.querySelector('[data-fig-step="1"]').disabled = !newer;
    nav.hidden = !older && !newer;
  }

  function figStep(dir) {
    const { own, group } = figHistories();
    const history = state.figScope === "konsern" ? group : own;
    if (state.figRange !== "all" && +state.figRange < history.length && history.length > 3) {
      state.figOff = (state.figOff || 0) - dir;
      return renderFigures();
    }
    const sc = $("figures").querySelector(".fig-scroll");
    const th = sc && sc.querySelector("thead th[data-col]");
    if (sc) sc.scrollBy({ left: dir * (th ? th.offsetWidth : 90), behavior: "smooth" });
  }

  /** Figures where two filings disagree or a sum fails, oldest first. */
  function figFlags(shown) {
    const out = [];
    for (const f of shown.slice().reverse()) {
      for (const [label, field] of Object.entries(FIELD)) {
        const ch = figChecks(f, field);
        if (ch && (ch.st === "check" || ch.st === "restated")) out.push({ year: f.year, field, label, st: ch.st });
      }
    }
    return out;
  }

  // ---------- why the next year is read too (once per browser) ----------
  const SEEN_CONTROL = "regnskap.seenControlRead";
  let ctlNote = null;
  function closeCtlNote() {
    if (ctlNote) ctlNote.remove();
    ctlNote = null;
  }

  function explainControlRead(year) {
    try { if (localStorage.getItem(SEEN_CONTROL)) return; } catch (e) { /* no storage: show it each time */ }
    const th = document.querySelector(`#figures thead th[data-col="${year}"]`);
    if (!th) return;
    try { localStorage.setItem(SEEN_CONTROL, "1"); } catch (e) { /* ignore */ }
    closeCtlNote();
    ctlNote = document.createElement("div");
    ctlNote.className = "fig-pop ctl-note";
    ctlNote.setAttribute("role", "dialog");
    ctlNote.setAttribute("aria-labelledby", "ctl-note-h");
    ctlNote.innerHTML = `<h3 id="ctl-note-h">Vi leser også ${year}</h3>
      <p>${year}-regnskapet viser ${year - 1}-tallene som fjorårstall. Vi sammenligner dem for å kontrollere ${year - 1}.</p>
      <p><button type="button" class="tool-btn" data-ctl-ok>Skjønner</button></p>`;
    document.body.appendChild(ctlNote);
    const r = th.getBoundingClientRect();
    const w = ctlNote.offsetWidth;
    ctlNote.style.left = `${Math.max(8, Math.min(window.scrollX + r.right - w, window.scrollX + document.documentElement.clientWidth - w - 8))}px`;
    ctlNote.style.top = `${window.scrollY + r.bottom + 6}px`;
    ctlNote.querySelector("[data-ctl-ok]").focus();
  }
  document.addEventListener("click", (e) => {
    if (ctlNote && (e.target.closest("[data-ctl-ok]") || !ctlNote.contains(e.target))) closeCtlNote();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeCtlNote(); });

  // ---------- a figure's sources ----------
  let figPop = null;
  function closeFigPop() {
    if (figPop) figPop.remove();
    figPop = null;
    document.querySelectorAll("td.src-cell.open").forEach((t) => t.classList.remove("open"));
  }

  function openFigPop(td) {
    closeFigPop();
    const year = +td.dataset.fy, field = td.dataset.ff, sign = +td.dataset.fs;
    const { own, group } = figHistories();
    const f = (state.figScope === "konsern" ? group : own).find((x) => x.year === year);
    const ch = f && figChecks(f, field);
    if (!ch) return;
    const show = (v) => (v === null || v === undefined ? "–" : k(sign < 0 ? -v : v));
    const next = accByYear().get(year + 1);
    const pdfNext = state.data.years.includes(year + 1) && !(next && next.status === "ok");
    const verdict = {
      ok: ["ok", f.pdf ? "Kildene er enige." : "Lik Brreg."],
      restated: ["amber", "Ulikt i neste års regnskap. Kan være omarbeidet; tabellen viser tallet slik det ble rapportert."],
      check: ["red", f.pdf ? "Må sjekkes mot PDF-en." : "Brreg og PDF-en er ulike. Tabellen viser Brregs tall."],
      one: ["", ch.src[0].col === "prior" ? `Bare én kilde: fjorårstallet i ${ch.src[0].year}-regnskapet.`
        : pdfNext ? `Fjorårstallet i ${year + 1}-regnskapet er ikke lest ennå.${busyRow(next) ? ""
          : ` <button type="button" class="lk" data-fig-check="${year + 1}">Les inn ${year + 1}</button>`}` : "Bare én kilde."],
    }[ch.st];
    figPop = document.createElement("div");
    figPop.className = "fig-pop";
    figPop.dataset.y = year;
    figPop.dataset.f = field;
    figPop.setAttribute("role", "dialog");
    figPop.innerHTML = `<button type="button" class="x" aria-label="Lukk">×</button><h3>${esc(td.dataset.fl)} ${year}</h3>
      <ul>${ch.src.map((s) => `<li><span>${s.col !== "brreg" && s.page
        ? `<button type="button" class="lk" data-fig-src="${s.year}:${s.page}" title="Åpne siden i leseren">${esc(sourceLabel(s))}</button>`
        : esc(sourceLabel(s))}</span><b>${show(s.v)}</b></li>`).join("")}</ul>
      ${ch.sum === true ? '<p class="muted">Summene i regnskapet går opp.</p>' : ch.sum === false ? '<p class="muted">En sum dette tallet inngår i, går ikke opp.</p>' : ""}
      <p class="verdict ${verdict[0]}">${verdict[1]}</p>`;
    document.body.appendChild(figPop);
    const r = td.getBoundingClientRect();
    const w = figPop.offsetWidth;
    figPop.style.left = `${Math.max(8, Math.min(window.scrollX + r.right - w, window.scrollX + document.documentElement.clientWidth - w - 8))}px`;
    figPop.style.top = `${window.scrollY + r.bottom + 6}px`;
    td.classList.add("open");
  }

  async function readFigYears(years) {
    const c = state.data.company;
    const { brregYears } = figHistories();
    const control = controlYears(years, brregYears, state.data.years, state.accRows);
    try {
      // Separate requests run side by side, so the checking year doesn't hold up the one asked for
      const posts = [years, ...(control.length ? [control] : [])].map((ys) => api("POST", `/api/company/${c.orgnr}/accounts`, { years: ys }));
      const res = await Promise.all(posts);
      state.accRows = res[res.length - 1].years;
      renderAccYears();
      renderFigures();
      pollAccounts();
      if (control.length) explainControlRead(control[0]);
    } catch (e) {
      setStatus(e.message || "Kunne ikke starte innlesingen.", true);
    }
  }

  $("fig-block").addEventListener("click", (e) => {
    if (e.target.closest("[data-fig-xlsx]")) return downloadFiguresXlsx();
    if (e.target.closest("[data-fig-copy]")) return copyFigures();
    const td = e.target.closest("td.src-cell");
    if (td) return td.classList.contains("open") ? closeFigPop() : openFigPop(td);
    const read = e.target.closest("[data-fig-read]");
    if (read) { read.disabled = true; state.figToLeft = !!read.closest(".fig-next"); return readFigYears([+read.dataset.figRead]); }
    if (e.target.closest("[data-fig-read-all]")) {
      const { own, group } = figHistories();
      const history = state.figScope === "konsern" ? group : own;
      const unread = unreadYears(state.data.years, history.map((f) => f.year), state.accRows).filter((y) => y < history[0].year);
      state.figRange = "all";
      return readFigYears(unread);
    }
    const step = e.target.closest("[data-fig-step]");
    if (step) {
      figStep(+step.dataset.figStep);
      $("fig-ctl").querySelector(`[data-fig-step="${step.dataset.figStep}"]`)?.focus();
      return;
    }
    if (e.target.closest("[data-fig-flag]")) {
      const { own, group } = figHistories();
      const history = state.figScope === "konsern" ? group : own;
      const flags = figFlags(history);
      if (!flags.length) return;
      const fl = flags[(state.figFlagIdx = ((state.figFlagIdx ?? -1) + 1) % flags.length)];
      const at = history.findIndex((f) => f.year === fl.year);
      if (state.figRange !== "all" && (at < state.figOff || at >= state.figOff + +state.figRange)) { state.figOff = Math.max(0, at - 1); renderFigures(); }
      const cellEl = $("figures").querySelector(`td[data-fy="${fl.year}"][data-ff="${fl.field}"]`);
      if (!cellEl) return;
      cellEl.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
      setTimeout(() => openFigPop(cellEl), 300);
      return;
    }
    const rangeBtn = e.target.closest("[data-fig-range]");
    if (rangeBtn) {
      state.figRange = rangeBtn.dataset.figRange;
      state.figOff = 0;
      storeSet("regnskap.figRange", state.figRange);
      return renderFigures();
    }
    const yearBtn = e.target.closest("[data-fig-year]");
    if (yearBtn) {
      state.figYear = +yearBtn.dataset.figYear;
      state.figView = "year";
    } else if (e.target.closest("[data-fig-all]")) {
      state.figView = "all";
    } else if (e.target.closest("[data-fig-scope]")) {
      state.figScope = e.target.closest("[data-fig-scope]").dataset.figScope;
      state.figOff = 0;
      return renderFigures();
    } else {
      return;
    }
    storeSet("regnskap.figView", state.figView);
    renderFigures();
  });
  $("fig-block").addEventListener("keydown", (e) => {
    const td = e.target.closest && e.target.closest("td.src-cell");
    if (td && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openFigPop(td); return; }
  });
  window.addEventListener("resize", figNavState);
  // ← → step through the years while the table is on screen and nothing else takes the keys
  document.addEventListener("keydown", (e) => {
    if ((e.key !== "ArrowLeft" && e.key !== "ArrowRight") || e.metaKey || e.ctrlKey || e.altKey || state.view !== "company") return;
    if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
    const nav = $("fig-ctl").querySelector(".fig-nav");
    const r = $("fig-block").getBoundingClientRect();
    if (!nav || nav.hidden || r.bottom < 0 || r.top > window.innerHeight) return;
    e.preventDefault();
    figStep(e.key === "ArrowLeft" ? -1 : 1);
  });
  document.addEventListener("click", (e) => {
    if (!figPop) return;
    const chk = e.target.closest("[data-fig-check]");
    if (chk) {
      closeFigPop();
      readFigYears([+chk.dataset.figCheck]);
      return;
    }
    const src = e.target.closest("[data-fig-src]");
    if (src) {
      const [year, page] = src.dataset.figSrc.split(":").map(Number);
      closeFigPop();
      go("accounts", { orgnr: state.data.company.orgnr, year, goto: { page } });
      return;
    }
    if (e.target.closest(".x") || (!figPop.contains(e.target) && !e.target.closest("td.src-cell"))) closeFigPop();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeFigPop(); });

  /** The accounts on screen (selskap or konsern, every year) as export sheets, or null. */
  function figureExport() {
    const scope = state.figScope === "konsern" ? "konsern" : "selskap";
    const { own, group } = figHistories();
    const history = scope === "konsern" ? group : own;
    return history && history.length ? { scope, history, sheets: exportSheets(history) } : null;
  }

  async function downloadFiguresXlsx() {
    const ex = figureExport();
    if (!ex) return;
    const filename = exportFilename(state.data.company.name, ex.scope, ex.history);
    setStatus("Lager Excel-fil …");
    try {
      const resp = await fetch("/api/xlsx", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename, sheets: ex.sheets }),
      });
      if (!resp.ok) throw new Error(await apiError(resp));
      const url = URL.createObjectURL(await resp.blob());
      triggerDownload(url, filename);
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      setStatus(`Lastet ned ${filename}`);
    } catch (e) {
      setStatus(e.message || "Excel-filen kunne ikke lages.", true);
    }
  }

  async function copyFigures() {
    const ex = figureExport();
    if (!ex) return;
    try {
      await navigator.clipboard.writeText(toTsv(ex.sheets));
      setStatus("Kopiert. Lim inn i Google Sheets eller Excel.");
    } catch (e) {
      setStatus("Nettleseren slapp ikke til utklippstavlen. Bruk «Last ned Excel».", true);
    }
  }


  // Roles
  function renderRoles() {
    const c = state.data.company;
    const items = (c.roles || []).map((r) => [r.role, r.orgnr
      ? `<button type="button" class="lk" data-open="${esc(r.orgnr)}">${esc(r.name)}</button> <small>${fmtOrgnr(r.orgnr)}</small>`
      : esc(r.name)]);
    if (c.capital && c.capital.amount !== null && c.capital.amount !== undefined) {
      const cur = c.capital.currency && c.capital.currency !== "NOK" ? ` ${esc(c.capital.currency)}` : " kr";
      items.push(["Aksjekapital", `${nf0.format(c.capital.amount)}${cur}${c.capital.shares ? ` · ${nf0.format(c.capital.shares)} aksjer` : ""}`]);
    }
    if (c.employees !== null && c.employees !== undefined) items.push(["Ansatte", nf0.format(c.employees)]);
    $("roles").innerHTML = items.length
      ? items.map(([kk, v]) => `<div><dt>${esc(kk)}</dt><dd>${v}</dd></div>`).join("")
      : '<p class="note">Ingen roller registrert.</p>';
  }

  // Ownership
  function renderOwnership() {
    const own = state.data.ownership || { owned_by: [], subsidiaries: { status: "none_yet" } };
    renderRegister();
    $("owned-by").innerHTML = own.owned_by && own.owned_by.length
      ? own.owned_by.map((o) => `<p class="owned"><b>Eies av</b> <button type="button" class="lk" data-open="${esc(o.orgnr)}">${esc(o.name)}</button>
          ${o.share_pct === null || o.share_pct === undefined ? "" : `<span class="num">${esc(o.share_pct)} %</span>`}
          <span class="muted">· fra ${esc(o.name)} sin årsrapport ${esc(o.source_year)}${o.source_page ? `, <a href="/api/pdf/${esc(o.orgnr)}/${esc(o.source_year)}#page=${esc(o.source_page)}" target="_blank" rel="noopener">s. ${esc(o.source_page)}</a>` : ""}</span></p>`).join("")
      : '<p class="owned"><b>Eies av</b> <span class="muted">Ukjent. Ingen innleste årsrapporter oppgir selskapet som datterselskap.</span></p>';
    $("subs").innerHTML = "";
    renderSubs(own.subsidiaries || { status: "none_yet" });
  }

  // Kunngjøringer: the newest few whatever their topic, one row per topic with what it is now (its history opens
  // under it), search, and every notice openable. Read from Brreg newest first: a visit reads VISIT_READS,
  // «Hent de N andre» the rest a few per call so the section fills in as they come, «Hent N» one topic's, and an
  // unread notice is read when opened. The server paces all of it.
  const STEP = 4;
  const VISIT_READS = 5;
  const LATEST = 5;
  const TOPIC_READS = 2; // a topic's unread notices read this many at a time

  async function loadNotices(orgnr) {
    const k = state.kn = { orgnr, d: null, error: null, open: new Set(), more: new Set(), pending: new Set(), all: false, q: "", busy: false, stop: false };
    $("kn-q").value = "";
    renderNotices();
    try {
      k.d = await api("GET", `/api/company/${orgnr}/kunngjoringer?batch=${VISIT_READS}`);
    } catch (e) {
      k.error = e.message || "Kunne ikke hente kunngjøringene.";
    }
    if (state.kn === k) renderNotices();
  }

  /** Read the rest, STEP at a time newest first, redrawing after each step, until done or stopped. */
  async function fillNotices(k) {
    if (k.busy || !k.d.unread) return;
    Object.assign(k, { busy: true, stop: false });
    renderNotices();
    try {
      while (k.d.unread && !k.stop && state.kn === k) {
        const before = k.d.unread;
        const d = await api("GET", `/api/company/${k.orgnr}/kunngjoringer?batch=${STEP}`);
        if (state.kn !== k) return;
        k.d = d;
        renderNotices();
        if (d.stale || d.unread >= before) break;
      }
    } catch (e) {
      setStatus(e.message || "Kunne ikke hente flere kunngjøringer.", true);
    }
    k.busy = false;
    if (state.kn === k) renderNotices();
  }

  /** Read these listed notices one by one (TOPIC_READS at a time), each shown as it arrives. */
  async function readNotices(k, kids) {
    const todo = kids.filter((kid) => !k.pending.has(kid));
    todo.forEach((kid) => k.pending.add(kid));
    renderNotices();
    const one = async (kid) => {
      try {
        const it = await api("GET", `/api/company/${k.orgnr}/kunngjoringer/${kid}`);
        if (state.kn !== k) return;
        const i = k.d.items.findIndex((x) => x.kid === kid);
        if (i >= 0) k.d.items[i] = it;
        k.d.unread = k.d.items.filter((x) => !x.read).length;
      } catch (e) {
        k.open.delete(kid);
        setStatus(e.message || "Kunne ikke hente kunngjøringen.", true);
      }
      k.pending.delete(kid);
      if (state.kn === k) renderNotices();
    };
    for (let i = 0; i < todo.length; i += TOPIC_READS) await Promise.all(todo.slice(i, i + TOPIC_READS).map(one));
  }

  const knBody = (it) => (it.read ? noticeHtml(it) : '<p class="working"><span class="spinner"></span> Henter fra Brreg …</p>');

  function noticeRow(it) {
    const k = state.kn, open = k.open.has(it.kid);
    const s = it.read ? valHtml(summary(it)) : k.pending.has(it.kid) ? '<span class="spinner"></span>' : '<span class="kn-unr">Ikke hentet · åpne for å hente</span>';
    return `<li><button type="button" class="kn-rw" data-kn="${esc(it.kid)}" aria-expanded="${open}">
      <span class="d num">${shortDate(it.date)}</span><span class="t">${esc(shortTitle(it))}</span><span class="s">${s}</span></button>
      ${open ? knBody(it) : ""}</li>`;
  }

  function topicRow(t) {
    const k = state.kn, ex = k.more.has(t.key), now = t.xs[0];
    let cur;
    if (t.other) {
      const types = [...new Set(t.all.map(shortTitle))];
      cur = `<span class="muted">${esc(types.slice(0, 3).join(", "))}${types.length > 3 ? " …" : ""}</span>`;
    } else if (now) {
      cur = `<b>${valHtml(now)}</b>`;
    } else {
      cur = t.all.some((it) => k.pending.has(it.kid)) ? '<span class="spinner"></span>'
        : `<span class="kn-unr">Ikke hentet</span> <button type="button" class="lk kn-sm" data-kn-get="${t.key}">Hent ${t.unread}</button>`;
    }
    const cnt = `${plural(t.all.length, "kunngjøring", "kunngjøringer")}${t.unread && t.unread < t.all.length ? `, ${t.unread} ikke hentet` : ""}`;
    const hist = !ex ? "" : `<ol class="kn-hist">${t.all.map((it) => {
      const x = t.other ? null : t.xs.find((y) => y.it === it);
      if (!t.other && it.read && !x) return ""; // same value as the notice before: folded into it
      const open = k.open.has(it.kid);
      const text = !it.read ? (k.pending.has(it.kid) ? '<span class="spinner"></span>' : `<span class="kn-unr">${esc(shortTitle(it))} · ikke hentet</span>`)
        : t.other ? `${esc(shortTitle(it))} <span class="muted">${valHtml(summary(it))}</span>` : valHtml(x);
      return `<li class="${x && x === now ? "now" : ""}"><button type="button" class="kn-rw" data-kn="${esc(it.kid)}" aria-expanded="${open}">
        <span class="d num">${shortDate(it.date)}</span><span class="t">${text}</span></button>${open ? knBody(it) : ""}</li>`;
    }).join("")}</ol>`;
    return `<li class="kn-topic${ex ? " ex" : ""}"><div class="kn-tr">
      <span class="tl">${esc(t.label)}</span><span class="tv">${cur}</span><span class="d num">${!t.other && now ? shortDate(now.it.date) : ""}</span>
      <button type="button" class="lk kn-sm kn-cnt" data-kn-more="${t.key}" aria-expanded="${ex}">${ex ? "Skjul" : cnt}</button></div>${hist}</li>`;
  }

  function renderNoticeLoad() {
    const k = state.kn, d = k && k.d;
    if (!d || !d.unread) { $("kn-load").innerHTML = ""; return; }
    const n = d.items.length, got = n - d.unread;
    const bar = `<span class="kn-bar" role="progressbar" aria-valuemin="0" aria-valuemax="${n}" aria-valuenow="${got}"><i style="width:${(got / n) * 100}%"></i></span>`;
    $("kn-load").innerHTML = k.busy
      ? `<span class="kn-load-t"><span class="spinner"></span><span><b class="num">${got} av ${n}</b> hentet · henter resten, nyeste først</span></span>${bar}
        <button type="button" class="secondary" id="kn-stop">Stopp</button>`
      : `<span class="kn-load-t"><span><b class="num">${got} av ${n}</b> hentet fra Brreg</span></span>${bar}
        <button type="button" class="primary sm" id="kn-rest">Hent de ${d.unread} andre</button>`;
  }

  function renderNotices() {
    const k = state.kn;
    if (!k) return;
    $("kn-n").textContent = "";
    $("kn-alert").innerHTML = "";
    renderNoticeLoad();
    if (k.error) { $("kn").innerHTML = `<p class="note">${esc(k.error)}</p>`; return; }
    if (!k.d) { $("kn").innerHTML = '<p class="working"><span class="spinner"></span> Henter kunngjøringer …</p>'; return; }
    const d = k.d, items = d.items, n = items.length;
    $("kn-n").textContent = n ? fmtInt(n) : "";
    $("kn-src").innerHTML = `Brønnøysundregistrene · <a href="${esc(d.source)}" target="_blank" rel="noopener">w2.brreg.no ↗</a>`;
    $("kn-q").placeholder = `Søk i alle ${fmtInt(n)} kunngjøringer`;
    if (!n) { $("kn").innerHTML = '<p class="note">Ingen kunngjøringer.</p>'; return; }
    const bad = bankruptcy(items);
    if (bad) {
      const facts = bankruptcyFacts(bad), open = k.open.has(bad.kid);
      $("kn-alert").innerHTML = `<div class="kn-konk"><h3>${esc(bankruptcyHead(bad))}</h3>
        ${facts.length ? `<dl>${facts.map(([l, v]) => `<div><dt>${esc(l)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>` : ""}
        <p class="acts"><button type="button" class="lk" data-kn="${esc(bad.kid)}" aria-expanded="${open}">${open ? "Skjul kunngjøringen" : "Les hele kunngjøringen"}</button></p>
        ${open ? knBody(bad) : ""}</div>`;
    }
    const stale = d.stale ? '<p class="note">Brreg svarer ikke akkurat nå. Viser det som er lagret.</p>' : "";
    if (k.q.trim().length >= 2) {
      const hits = searchNotices(items, k.q);
      $("kn").innerHTML = `<h3 class="kn-sh">${plural(hits.length, "treff", "treff")} <span class="muted">av ${fmtInt(n)}</span></h3>
        ${d.unread ? `<p class="kn-hint">${fmtInt(d.unread)} er ikke hentet, så søket ser bare hva slags kunngjøring de er.${k.busy ? "" : ' <button type="button" class="lk" data-kn-rest>Hent alle</button> for å søke i teksten.'}</p>` : ""}
        <ol class="kn-rows">${hits.map(noticeRow).join("") || '<li class="kn-none">Ingen treff.</li>'}</ol>${stale}`;
      return;
    }
    $("kn").innerHTML = `<h3 class="kn-sh">Siste</h3><ol class="kn-rows">${items.slice(0, LATEST).map(noticeRow).join("")}</ol>
      <h3 class="kn-sh">Per tema <span class="muted">· hva det er nå</span></h3><ol class="kn-topics">${tracks(items).map(topicRow).join("")}</ol>
      <div class="kn-allrow"><button type="button" class="secondary" id="kn-all" aria-expanded="${k.all}">${k.all ? "Skjul listen" : `Alle ${fmtInt(n)} kunngjøringer`}</button>
        ${k.all ? "" : '<span class="muted">nyest først, hver kan åpnes</span>'}</div>
      ${k.all ? `<ol class="kn-rows">${items.map(noticeRow).join("")}</ol>` : ""}${stale}`;
  }

  $("kn-block").addEventListener("click", (e) => {
    const k = state.kn;
    if (!k || !k.d) return;
    const n = e.target.closest("[data-kn]");
    if (n) {
      const kid = n.dataset.kn;
      if (k.open.has(kid)) { k.open.delete(kid); renderNotices(); return; }
      k.open.add(kid);
      const it = k.d.items.find((x) => x.kid === kid);
      if (it && !it.read) readNotices(k, [kid]);
      else renderNotices();
      return;
    }
    const more = e.target.closest("[data-kn-more]");
    if (more) { const key = more.dataset.knMore; k.more.has(key) ? k.more.delete(key) : k.more.add(key); renderNotices(); return; }
    const get = e.target.closest("[data-kn-get]");
    if (get) {
      const t = tracks(k.d.items).find((x) => x.key === get.dataset.knGet);
      if (t) readNotices(k, t.all.filter((it) => !it.read).map((it) => it.kid));
      return;
    }
    if (e.target.closest("#kn-rest, [data-kn-rest]")) { fillNotices(k); return; }
    if (e.target.closest("#kn-stop")) { k.stop = true; renderNotices(); return; }
    if (e.target.closest("#kn-all")) { k.all = !k.all; renderNotices(); }
  });
  $("kn-q").addEventListener("input", (e) => {
    if (!state.kn) return;
    state.kn.q = e.target.value;
    renderNotices();
  });

  // Shareholder register (aksjonærregisteret): owners, holdings, changes.

  function fmtChange(c) {
    const a = fmtPct(c.from_pct), b = fmtPct(c.to_pct);
    const pcts = a === b ? b : `${a} → ${b}`;
    if (c.from_shares === c.to_shares) return `samme ${fmtInt(c.to_shares)} aksjer, selskapets aksjetall endret (${pcts})`;
    return a === b ? `${fmtInt(c.from_shares)} → ${fmtInt(c.to_shares)} aksjer (${b})` : pcts;
  }
  function holderId(h, withOrgnr = false) {
    if (h.orgnr && state.data && h.orgnr === state.data.company.orgnr) return "egne aksjer";
    if (h.orgnr && !norwegian(h.orgnr)) return withOrgnr ? `utenlandsk · ${h.orgnr}` : "utenlandsk";
    // The name links to the company, so the org.nr only earns its place telling same-name rows apart.
    if (h.orgnr) return withOrgnr ? `org.nr ${fmtOrgnr(h.orgnr)}` : "";
    if (h.birth_year) return `f. ${h.birth_year}`;
    return h.country && h.country !== "NO" ? "utenlandsk" : "";
  }
  // Muted note after a name ("f. 1961", "egne aksjer", a share class); nothing when it's empty.
  const idTag = (...parts) => {
    const t = parts.filter(Boolean).join(" · ");
    return t ? ` <span class="id">${esc(t)}</span>` : "";
  };
  function holderPlace(h) {
    const p = [h.postcode, h.place].filter(Boolean).join(" ");
    return h.country && h.country !== "NO" ? [p, h.country].filter(Boolean).join(", ") : p;
  }
  // `text` is a shortened name (the eierkart drops the part siblings share); the full one is the title.
  function holderName(h, text = h.name) {
    const self = state.data && h.orgnr === state.data.company.orgnr;
    const title = text !== h.name ? ` title="${esc(h.name)}"` : "";
    if (h.orgnr && !self && norwegian(h.orgnr)) return `<button type="button" class="lk" data-open="${esc(h.orgnr)}"${title}>${esc(text)}</button>`;
    if (!h.orgnr && h.name) return `<button type="button" class="lk" data-person="${esc(h.name)}" data-born="${h.birth_year || ""}"${title}>${esc(text)}</button>`;
    return esc(text || "Uten navn");
  }

  async function loadRegister(orgnr, year) {
    const seq = ++state.regSeq;
    try {
      const r = await api("GET", `/api/company/${orgnr}/shareholders${year ? `?year=${year}` : ""}`);
      if (seq !== state.regSeq || state.orgnr !== orgnr) return;
      state.reg = r;
    } catch (e) {
      if (seq !== state.regSeq || state.orgnr !== orgnr) return;
      state.reg = { error: e.message || "Kunne ikke hente aksjonærregisteret." };
    }
    renderRegister();
  }

  function renderRegister() {
    const r = state.reg;
    $("owned-by").hidden = !!(r && r.found);
    if (!r) {
      $("reg-src").textContent = "Aksjonærregisteret";
      $("reg").innerHTML = '<p class="working"><span class="spinner"></span> Henter aksjonærer …</p>';
      return;
    }
    if (r.error || !r.found) {
      $("reg-src").textContent = "Aksjonærregisteret";
      $("reg").innerHTML = `<p class="note">${r.error ? esc(r.error) : `Ikke i aksjonærregisteret ${r.years[0]}–${r.years[r.years.length - 1]}. Bare aksjeselskaper er med.`}</p>`;
      return;
    }
    $("reg-src").innerHTML = `Aksjonærregisteret <span class="reg-years" id="reg-years">${r.years.map((y) =>
      `<button type="button" data-reg-year="${y}" aria-pressed="${y === r.year}">${y}</button>`).join("")}</span>`;
    const c = state.data.company;
    $("reg").innerHTML = miniMap(r, { name: c.name, open: `data-structure="${esc(c.orgnr)}" data-year="${r.year}"`, what: "selskapsstruktur" })
      || `<p class="note">Ingen eiere med minst 5 % og ingen aksjer i andre selskaper i ${r.year}.</p>`;
  }

  // Mini map: direct owners (≥ 5 %) → the company or person → its biggest branches, and the
  // button to the whole structure (made once, then saved). `r` is a register snapshot or a
  // person; `root` = {name, open (attributes that open the full map), what (its name)}.
  const MAP_OWNERS = 3;
  const fmtDate = (iso) => new Date(iso).toLocaleDateString("nb-NO", { day: "numeric", month: "short", year: "numeric" });

  function miniMap(r, root) {
    const chain = r.chain || [];
    const level = chain[0] || [];
    const owners = level.filter((o) => !o.more);
    const g = r.group;
    if (!owners.length && !g) return "";
    const open = root.open;
    const above = (o) => (chain[1] || []).filter((x) => x.of === o.orgnr).reduce((n, x) => n + (x.more || 1), 0);
    const ownerBox = (o) => {
      const n = o.orgnr ? above(o) : 0;
      const sub = n ? `${n} ${n === 1 ? "eier" : "eiere"} over` : holderId(o);
      return `<div class="mbox${o.birth_year ? " person" : ""}">${stakeLabel(o.pct)}${holderName(o)}${sub ? `<span class="sub">${esc(sub)}</span>` : ""}</div>`;
    };
    const extra = owners.length - Math.min(owners.length, MAP_OWNERS) + level.reduce((n, o) => n + (o.more || 0), 0);
    const up = owners.slice(0, MAP_OWNERS).map(ownerBox)
      .concat(extra ? [`<button type="button" class="mbox more" ${open}><span class="p">+ ${extra}</span><span class="sub">eiere til med minst 5 %</span></button>`] : []);
    const down = g ? g.branches.map((b) => `<div class="mbox${b.kind === "own" ? "" : " minor"}">${stakeLabel(b.pct, b.kind !== "own")}
        <button type="button" class="lk" data-open="${esc(b.orgnr)}">${esc(b.name)}</button>${b.under ? `<span class="sub">${fmtInt(b.under)} under</span>` : ""}</div>`)
      .concat(g.rest ? [`<button type="button" class="mbox more" ${open}><span class="p">+ ${g.rest}</span><span class="sub">direkte eide${g.rest_under ? `, ${fmtInt(g.rest_under)} under` : ""}</span></button>`] : [])
      : [];
    const rule = [];
    if (owners.length) rule.push("Oppover: eiere med minst 5 %.");
    if (g) rule.push(`Nedover: selskaper eid med ${r.follow_rule}, og deres selskaper med ${r.follow_rule} videre ned. Det er dem «under» teller. Eierandeler ${r.follow_below} vises stiplet; i kartet kan du velge å følge dem videre.`);
    const rootSub = !g ? "Eier ingen aksjer i andre selskaper"
      : g.depth > 1 ? `${fmtInt(g.direct)} direkte · ${fmtInt(g.companies)} selskaper i ${g.depth} ledd`
      : `Eier aksjer i ${fmtInt(g.direct)} ${g.direct === 1 ? "selskap" : "selskaper"}`;
    const saved = r.structure_saved;
    return `<div class="omap">
        ${up.length ? `<div class="mrow up" style="--n:${up.length}">${up.join("")}</div>` : ""}
        <div class="mroot${up.length ? " has-up" : ""}${down.length ? " has-down" : ""}"><b>${esc(root.name)}</b><span>${rootSub}</span></div>
        ${down.length ? `<div class="mrow down" style="--n:${down.length}">${down.join("")}</div>` : ""}
      </div>
      <p class="note"><span class="on-line">Eierandelen står på linjen. </span>${esc(rule.join(" "))}</p>
      <div class="sel-row"><button type="button" class="primary" ${open}>Vis ${root.what} →</button>
        <span class="muted">${saved ? `Lagret ${esc(fmtDate(saved))}` : "Lages første gang og lagres"}</span></div>`;
  }

  $("own-block").addEventListener("click", (e) => {
    const y = e.target.closest("[data-reg-year]");
    if (y) loadRegister(state.orgnr, Number(y.dataset.regYear));
  });

  // Person view: what one shareholder (name + birth year) owns, by year.
  async function openPerson(arg) {
    showView("person");
    state.person = { name: arg.name, born: arg.born, data: null, histOpen: false };
    document.title = `${arg.name} · Årsregnskap`;
    renderPerson();
    loadPerson(arg.year);
  }

  async function loadPerson(year) {
    const pr = state.person;
    const q = new URLSearchParams({ name: pr.name });
    if (pr.born) q.set("born", pr.born);
    if (year) q.set("year", year);
    try {
      const d = await api("GET", `/api/person?${q}`);
      if (state.person !== pr) return;
      pr.data = d;
    } catch (e) {
      if (state.person !== pr) return;
      pr.data = { error: e.message || "Kunne ikke hente aksjonærregisteret." };
    }
    renderPerson();
  }

  function renderPerson() {
    const pr = state.person, d = pr.data;
    $("p-name").textContent = pr.name;
    const facts = [];
    if (pr.born) facts.push(`f. ${pr.born}`);
    if (d && d.address) facts.push(`${esc(holderPlace(d.address))} <span class="muted">(${d.address.year})</span>`);
    facts.push("Aksjonærregisteret");
    $("p-facts").innerHTML = facts.join(" · ");
    if (!d) { $("p-src").textContent = ""; $("p-body").innerHTML = '<p class="working"><span class="spinner"></span> Henter aksjer …</p>'; return; }
    if (d.error) { $("p-src").textContent = ""; $("p-body").innerHTML = `<p class="note danger">${esc(d.error)}</p>`; return; }
    const span = `${d.years[0]}–${d.years[d.years.length - 1]}`;
    if (!d.found) {
      $("p-src").textContent = "";
      $("p-body").innerHTML = `<p class="note">Ingen aksjer registrert på ${esc(pr.name)}${pr.born ? ` f. ${pr.born}` : ""} i ${span}.</p>`;
      return;
    }
    $("p-src").innerHTML = `<span class="reg-years">${d.years.map((y) => `<button type="button" data-p-year="${y}" aria-pressed="${y === d.year}">${y}</button>`).join("")}</span>`;
    const co = (h) => `<button type="button" class="lk" data-open="${esc(h.orgnr)}">${esc(h.name)}</button>`;
    const verb = (c) => c.kind === "in" ? `inn i ${co(c)} med ${fmtPct(c.to_pct)}`
      : c.kind === "out" ? `ute av ${co(c)} (hadde ${fmtPct(c.from_pct)})` : `${co(c)}: ${fmtChange(c)}`;
    const hist = d.n_changes
      ? `<button type="button" class="lk" id="p-hist-toggle" aria-expanded="${pr.histOpen}" aria-controls="p-hist">Endringer ${span} (${d.n_changes})</button>` : "";
    const open = `data-pstructure="${esc(pr.name)}" data-born="${pr.born || ""}" data-year="${d.year}"`;
    $("p-body").innerHTML = `<p class="note">Registeret har ikke fødselsnummer. Treffene gjelder alle med navnet ${esc(pr.name)}${pr.born ? ` og fødselsår ${pr.born}` : ""}.</p>
      ${miniMap(d, { name: pr.name, open, what: "eierstruktur" }) || `<p class="note">Ingen aksjer registrert i ${d.year}.</p>`}
      <p class="note">Antall selskaper per år: ${d.years.map((y) => `${y}: ${d.counts[y] || 0}`).join(" · ")}</p>
      ${hist ? `<div class="reg-head">${hist}</div>` : ""}
      <div id="p-hist" class="reg-hist" ${pr.histOpen ? "" : "hidden"}><ul class="changes">${d.changes.map((c) =>
        `<li><span class="y num">${c.year}</span><span>${verb(c)}</span></li>`).join("")}</ul></div>`;
  }

  // Everyone matching a person search (the dropdown's «Alle N personer»): the register has no
  // national ID, so a common name is many people, told apart by birth year and place.
  async function openPeople(arg) {
    showView("people");
    const pl = state.people = { q: arg.q, data: null, filter: "" };
    document.title = `Personer: ${arg.q} · Årsregnskap`;
    $("pl-filter").value = "";
    renderPeople();
    try {
      const d = await api("GET", `/api/people?q=${encodeURIComponent(arg.q)}`);
      if (state.people === pl) pl.data = d;
    } catch (e) {
      if (state.people === pl) pl.data = { error: e.message || "Kunne ikke søke i aksjonærregisteret." };
    }
    if (state.people === pl) renderPeople();
  }

  function renderPeople() {
    const pl = state.people, d = pl.data;
    $("pl-title").textContent = `Personer: «${pl.q}»`;
    if (!d) { $("pl-facts").textContent = ""; $("pl-count").textContent = ""; $("pl-body").innerHTML = '<p class="working"><span class="spinner"></span> Søker i aksjonærregisteret …</p>'; return; }
    if (d.error) { $("pl-body").innerHTML = `<p class="note danger">${esc(d.error)}</p>`; return; }
    if (!d.ready) { $("pl-body").innerHTML = '<p class="note">Personsøket gjøres klart. Prøv igjen om et par minutter.</p>'; return; }
    const shown = d.people.length;
    $("pl-facts").textContent = d.capped
      ? `Over ${fmtInt(d.total)} personer har «${pl.q}» i navnet. Her er de ${fmtInt(shown)} første: skriv mer av navnet for å snevre inn.`
      : `${plural(d.total, "person", "personer")} med «${pl.q}» i navnet${d.total > shown ? `, de ${fmtInt(shown)} første vist` : ""}. Fra Aksjonærregisteret, som skiller folk med samme navn bare på fødselsår.`;
    const f = pl.filter.trim().toLocaleUpperCase("nb");
    const rows = d.people.filter((p) => !f || [p.name, String(p.birth_year), holderPlace(p)].some((t) => t.toLocaleUpperCase("nb").includes(f)));
    $("pl-count").textContent = f ? `${fmtInt(rows.length)} av ${fmtInt(shown)}` : "";
    $("pl-body").innerHTML = `<div class="list-wrap"><table class="subs reg-t"><thead><tr><th>Navn</th><th class="r">Født</th><th>Sted</th><th class="r">Aksjer i</th></tr></thead>
      <tbody>${rows.map((p) => `<tr><td>${holderName(p)}</td><td class="r">${p.birth_year}</td><td>${esc(holderPlace(p))}</td>
        <td class="r">${p.current ? plural(p.companies, "selskap", "selskaper") : `<span class="muted">til ${p.last_year}</span>`}</td></tr>`).join("")
      || `<tr><td colspan="4" class="muted">Ingen treff på «${esc(pl.filter)}».</td></tr>`}</tbody></table></div>`;
  }

  $("pl-filter").addEventListener("input", (e) => {
    if (!state.people || !state.people.data) return;
    state.people.filter = e.target.value;
    renderPeople();
  });

  $("v-person").addEventListener("click", (e) => {
    const y = e.target.closest("[data-p-year]");
    if (y) { loadPerson(Number(y.dataset.pYear)); return; }
    if (e.target.closest("#p-hist-toggle")) {
      state.person.histOpen = !state.person.histOpen;
      e.target.closest("#p-hist-toggle").setAttribute("aria-expanded", String(state.person.histOpen));
      $("p-hist").hidden = !state.person.histOpen;
    }
  });

  // Selskapsstruktur (a company) or eierstruktur (a person): the map from eierkart.js (layout)
  // drawn into a frame that pans and zooms. `arg` = {orgnr, year} or {person, born, year}.
  function structureUrl(arg, year = arg.year) {
    if (!arg.person) return `/api/company/${arg.orgnr}/structure${year ? `?year=${year}` : ""}`;
    const q = new URLSearchParams({ name: arg.person });
    if (arg.born) q.set("born", arg.born);
    if (year) q.set("year", year);
    return `/api/person/structure?${q}`;
  }
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const mapEl = $("s-map"), worldEl = $("s-world"), miniEl = $("s-mini");

  async function openStructure(arg) {
    showView("structure");
    const s = state.struct = { arg, orgnr: arg.orgnr || null, year: arg.year || null, data: null, mode: "show" };
    const b = $("s-back");
    ["open", "person", "born"].forEach((k) => delete b.dataset[k]);
    if (arg.person) Object.assign(b.dataset, { person: arg.person, born: arg.born || "" });
    else b.dataset.open = arg.orgnr;
    $("s-title").textContent = arg.person ? "Eierstruktur" : "Selskapsstruktur";
    b.textContent = arg.person ? `← ${arg.person}` : "← Tilbake til selskapet";
    b.title = `Tilbake til ${arg.person ? "personen" : "selskapet"} (Esc)`;
    $("s-years").innerHTML = "";
    $("s-hits").textContent = "";
    $("s-map").hidden = true;
    $("s-q").value = "";
    setMapHelp(false);
    const what = arg.person ? "eierstrukturen" : "selskapsstrukturen";
    $("s-msg").innerHTML = `<p class="working"><span class="spinner"></span> Henter ${what} …</p>`;
    renderChatContext();
    try {
      let d;
      try {
        d = await api("GET", structureUrl(arg));
      } catch (e) {
        if (e.status !== 404 || state.struct !== s) throw e;
        $("s-msg").innerHTML = `<p class="working"><span class="spinner"></span> Lager ${what} … Den lagres, så neste gang åpnes den med en gang.</p>`;
        d = await api("POST", structureUrl(arg));
      }
      if (state.struct === s) setStructure(s, d);
    } catch (e) {
      if (state.struct === s) $("s-msg").innerHTML = `<p class="note danger">${esc(e.message || `Kunne ikke lage ${what}.`)}</p>`;
    }
  }

  function setStructure(s, d) {
    s.data = d;
    indexDown(s);
    s.upParent = treeIndex(d.up, d.orgnr).parent;
    setMode(s, s.mode);
    Object.assign(s, { k: 1, tx: 0, ty: 0, W: 0, H: 0, pos: {}, at: {}, spots: {}, rects: [], hits: [], extra: {}, touched: false });
    resetStructure(s);
    const person = d.kind === "person";
    document.title = `${person ? "Eierstruktur" : "Selskapsstruktur"}: ${d.name || d.orgnr} · Årsregnskap`;
    $("s-back").textContent = `← ${d.name || d.orgnr}`;
    $("s-years").innerHTML = d.years.map((y) => `<button type="button" data-s-year="${y}" aria-pressed="${y === d.year}">${y}</button>`).join("");
    const when = new Date(d.created_at).toLocaleString("nb-NO", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
    $("s-saved").innerHTML = `Lagret ${esc(when)} · <button type="button" class="lk" id="s-redo">Lag på nytt</button>`;
    const st = d.stats;
    const ownerPct = `${nf0.format(d.rules.owner_pct)} %`;
    $("s-left").textContent = person ? ""
      : st.owners ? `← Eies av: ${plural(st.owners, "eier", "eiere")} i ${st.up_depth} ledd, hver med minst ${ownerPct}` : `← Ingen eiere med minst ${ownerPct}`;
    $("s-right").textContent = st.companies
      ? `Eier: ${plural(st.companies, "selskap", "selskaper")} i ${st.down_depth} ledd, eid med ${d.rules.follow} →`
      : `Eier ingen selskaper med ${d.rules.follow} →`;
    $("s-mode-label").textContent = `Eierandeler ${d.rules.below}`;
    $("s-legend-small").textContent = `Eierandel ${d.rules.below}, eller eierskap som går i ring`;
    $("s-note").textContent = "Eierandelen står på linjen mellom eieren og selskapet den eier."
      + ` Nedover følges selskaper eid med ${d.rules.follow}, ledd for ledd; eierandeler ${d.rules.below} vises stiplet, og selskapene under dem er ikke med i tallene.`
      + ` Velg «Følg» for å kunne åpne dem også.`
      + (person ? "" : ` Oppover vises eiere med minst ${ownerPct} i hvert ledd.`)
      + " Står et selskap flere steder i kartet, vises det helt på hvert sted."
      + " Går eierskapet i ring (A eier B, som eier A), stopper linjen der, merket «går i ring»."
      + (st.truncated ? " Kartet er kuttet: det var for stort til å vises helt." : "");
    $("s-msg").innerHTML = "";
    $("s-map").hidden = false;
    renderChatContext();
    renderStructure(s);
    fitStructure(s, true);
    // Tile widths are measured in Archivo; measure again if it wasn't loaded yet.
    if (document.fonts && document.fonts.status !== "loaded") {
      document.fonts.ready.then(() => {
        if (state.struct !== s) return;
        renderStructure(s, { keep: s.touched ? d.orgnr : null });
        if (!s.touched) fitStructure(s, true);
      });
    }
  }

  // First parent of every company downwards, so search can open the way to it.
  function indexDown(s) {
    s.parent = treeIndex(s.data.down, s.data.orgnr, s.follow).parent;
  }

  // Stakes under the line: "hide", "show" (dashed, not opened) or "follow" (each can be opened).
  function setMode(s, mode) {
    s.mode = mode;
    s.small = mode !== "hide";
    s.follow = mode === "follow";
    $("s-mode").querySelectorAll("[data-smode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.smode === mode)));
    indexDown(s);
  }

  async function followStake(s, key, btn) {
    btn.disabled = true;
    btn.textContent = "Henter …";
    try {
      const sub = await api("GET", `/api/company/${key}/holdings-tree?year=${s.data.year}`);
      if (state.struct !== s) return;
      mergeFollow(s.data, sub);
      indexDown(s);
      const id = btn.dataset.mid;
      s.open[id] = true;
      s.touched = true;
      renderStructure(s, { keep: id });
      // The company and what it holds, in the middle of the frame.
      const a = s.at[id], b = s.kidsBox[id];
      if (a) {
        const top = b ? Math.min(a.y, b.y) : a.y;
        panTo(s, b ? { x: a.x, y: top, w: b.x + b.w - a.x, h: Math.max(a.y + a.h, b.y + b.h) - top } : a, true);
      }
    } catch (err) {
      setStatus(err.message || "Kunne ikke hente eierandelene.", true);
      btn.disabled = false;
      btn.textContent = "▸ Prøv igjen";
    }
  }

  function resetStructure(s) {
    // This company open one level down; owners open all the way up (they're few).
    s.level = 1;
    s.open = {};
    s.upOpen = {};
    s.q = "";
    s.hitIdx = -1;
    $("s-q").value = "";
  }

  let measureCtx = null;
  function textWidth(text, condensed, weight = 600, size = 12) {
    measureCtx = measureCtx || document.createElement("canvas").getContext("2d");
    measureCtx.font = `${weight} ${size}px Archivo, system-ui, sans-serif`;
    const native = "fontStretch" in measureCtx;
    if (native) measureCtx.fontStretch = condensed ? "condensed" : "normal";
    const w = measureCtx.measureText(text).width;
    return native || !condensed ? w : w * 0.8;
  }

  /** Re-lay the map. `keep` (a place id) holds that box still on screen while the layout moves;
   * with `reveal` the companies it just opened are moved into view as well. */
  function renderStructure(s, { keep = null, reveal = null } = {}) {
    const before = keep && s.at[keep] ? s.at[keep] : null;
    const L = layoutMap(s, { width: mapEl.clientWidth, height: mapEl.clientHeight, measure: textWidth, nameHtml: holderName });
    Object.assign(s, { pos: L.pos, at: L.at, spots: L.spots, rects: L.rects, W: L.W, H: L.H, hits: L.hits, cand: L.cand, kidsBox: L.kidsBox });
    $("s-cap").hidden = !L.capped;
    worldEl.style.width = `${L.W}px`;
    worldEl.style.height = `${L.H}px`;
    $("s-lines").setAttribute("width", L.W);
    $("s-lines").setAttribute("height", L.H);
    $("s-lines").innerHTML = `<path d="${L.lines.join("")}"/>`;
    $("s-nodes").innerHTML = L.html.join("");
    if (before && L.at[keep]) {
      s.tx += (before.x - L.at[keep].x) * s.k;
      s.ty += (before.y - L.at[keep].y) * s.k;
    }
    if (reveal && L.kidsBox[reveal] && L.at[reveal]) {
      const r = L.kidsBox[reveal];
      const over = (r.x + r.w) * s.k + s.tx - (mapEl.clientWidth - 16);
      const room = L.at[reveal].x * s.k + s.tx - 16;
      if (over > 0) s.tx -= Math.max(0, Math.min(over, room));
    }
    if (s.hitIdx >= s.hits.length) s.hitIdx = s.hits.length ? 0 : -1;
    renderHits(s);
    applyView(s);
    markHit(s);
  }

  function applyView(s, animate = false) {
    const vw = mapEl.clientWidth, vh = mapEl.clientHeight;
    Object.assign(s, clampView(s, s.W, s.H, vw, vh));
    clearTimeout(s.animTimer);
    worldEl.classList.toggle("anim", animate && !reducedMotion.matches);
    if (animate) s.animTimer = setTimeout(() => worldEl.classList.remove("anim"), 400);
    worldEl.style.transform = `translate(${s.tx}px, ${s.ty}px) scale(${s.k})`;
    $("s-zv").textContent = `${Math.round(s.k * 100)} %`;
    miniEl.hidden = s.tx >= -1 && s.ty >= -1 && s.tx + s.W * s.k <= vw + 1 && s.ty + s.H * s.k <= vh + 1;
    drawMiniMap(s);
  }

  function zoomAt(s, sx, sy, f, animate = false) {
    const k = Math.min(2, Math.max(0.15, s.k * f));
    s.tx = sx - (sx - s.tx) * (k / s.k);
    s.ty = sy - (sy - s.ty) * (k / s.k);
    s.k = k;
    applyView(s, animate);
  }
  const zoomMid = (s, f) => zoomAt(s, mapEl.clientWidth / 2, mapEl.clientHeight / 2, f, true);

  /** The whole map in the window; when that would make it too small to read on opening, the
   * company at a readable size instead (the mini map shows the rest). */
  function fitStructure(s, opening = false) {
    Object.assign(s, fitView(s.W, s.H, mapEl.clientWidth, mapEl.clientHeight, s.pos[s.data.orgnr], opening));
    applyView(s, !opening);
  }

  function panTo(s, r, animate) {
    if (s.k < 0.6) s.k = 0.9;
    s.tx = mapEl.clientWidth / 2 - (r.x + r.w / 2) * s.k;
    s.ty = mapEl.clientHeight / 2 - (r.y + r.h / 2) * s.k;
    applyView(s, animate);
  }

  function drawMiniMap(s) {
    if (miniEl.hidden || !s.W) return;
    const cs = getComputedStyle(document.documentElement), col = (v) => cs.getPropertyValue(v).trim();
    const m = Math.min(200 / s.W, 130 / s.H);
    const w = Math.max(40, Math.round(s.W * m)), h = Math.max(30, Math.round(s.H * m));
    const dpr = window.devicePixelRatio || 1;
    if (miniEl.width !== Math.round(w * dpr) || miniEl.height !== Math.round(h * dpr)) {
      miniEl.width = Math.round(w * dpr);
      miniEl.height = Math.round(h * dpr);
      miniEl.style.width = `${w}px`;
      miniEl.style.height = `${h}px`;
    }
    s.miniM = m;
    const c = miniEl.getContext("2d");
    c.setTransform(dpr * m, 0, 0, dpr * m, 0, 0);
    c.fillStyle = col("--bg");
    c.fillRect(0, 0, s.W, s.H);
    for (const r of s.rects) {
      if (r.type === "block") { c.fillStyle = col("--hover"); c.fillRect(r.x, r.y, r.w, r.h); continue; }
      c.fillStyle = r.type === "root" ? col("--fg") : col("--soft");
      if (r.type === "tile") c.fillRect(r.x + 14, r.y + 6, r.w - 24, r.h - 12);
      else c.fillRect(r.x, r.y, r.w, r.h);
    }
    c.fillStyle = col("--red");
    for (const k of s.hits) for (const r of s.spots[k]) c.fillRect(r.x, r.y, Math.max(r.w, 4 / m), Math.max(r.h, 4 / m));
    c.strokeStyle = col("--red");
    c.lineWidth = 2 / m;
    c.strokeRect(-s.tx / s.k, -s.ty / s.k, mapEl.clientWidth / s.k, mapEl.clientHeight / s.k);
  }

  function renderHits(s) {
    const el = $("s-hits");
    if (s.q.trim().length < 2) { el.textContent = ""; return; }
    el.textContent = !s.hits.length ? "Ingen treff" : s.hitIdx >= 0 ? `${s.hitIdx + 1} av ${fmtInt(s.hits.length)} treff` : `${fmtInt(s.hits.length)} treff`;
  }
  function markHit(s) {
    $("s-nodes").querySelectorAll(".cur").forEach((e) => e.classList.remove("cur"));
    const key = s.hits[s.hitIdx];
    if (key) $("s-nodes").querySelectorAll(`[data-k="${CSS.escape(key)}"]`).forEach((e) => e.classList.add("cur"));
  }
  function nextHit(s, dir) {
    if (!s.hits.length) return;
    s.hitIdx = (s.hitIdx + dir + s.hits.length) % s.hits.length;
    panTo(s, s.pos[s.hits[s.hitIdx]], true);
    renderHits(s);
    markHit(s);
  }
  function clearMapSearch(s) {
    s.q = "";
    s.hitIdx = -1;
    $("s-q").value = "";
    renderStructure(s, { keep: s.data.orgnr });
  }

  function setMapHelp(open) {
    const pop = $("s-help-pop"), btn = $("s-help");
    pop.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (open) pop.style.left = `${Math.max(0, Math.min(btn.offsetLeft, btn.parentElement.clientWidth - pop.offsetWidth))}px`;
  }

  // The map's handlers act on the open eierkart, once it has loaded.
  const mapState = () => (state.view === "structure" && state.struct && state.struct.data ? state.struct : null);

  $("s-nodes").addEventListener("click", (e) => {
    const s = mapState();
    const f = e.target.closest("[data-follow]");
    if (s && f && !s.dragged) { followStake(s, f.dataset.follow, f); return; }
    const t = e.target.closest("[data-mkey]");
    if (!s || !t || s.dragged) return;
    const side = Number(t.dataset.mside), id = t.dataset.mkey;
    const opening = t.getAttribute("aria-expanded") !== "true";
    (side > 0 ? s.open : s.upOpen)[id] = opening;
    s.touched = true;
    renderStructure(s, { keep: id, reveal: opening && side > 0 ? id : null });
  });
  $("s-q").addEventListener("input", (e) => {
    const s = mapState();
    if (!s) return;
    s.q = e.target.value;
    s.hitIdx = 0;
    renderStructure(s, { keep: s.data.orgnr });
    if (s.hits.length) panTo(s, s.pos[s.hits[0]], false);
    else s.hitIdx = -1;
    renderHits(s);
    markHit(s);
  });
  $("s-q").addEventListener("keydown", (e) => {
    const s = mapState();
    if (!s) return;
    if (e.key === "Enter") { e.preventDefault(); nextHit(s, e.shiftKey ? -1 : 1); }
    else if (e.key === "Escape") { e.preventDefault(); if (s.q) clearMapSearch(s); else e.target.blur(); }
  });
  $("s-mode").addEventListener("click", (e) => {
    const s = mapState();
    const b = e.target.closest("[data-smode]");
    if (!s || !b || b.dataset.smode === s.mode) return;
    setMode(s, b.dataset.smode);
    renderStructure(s, { keep: s.data.orgnr });
  });
  $("s-deeper").addEventListener("click", () => {
    const s = mapState();
    if (!s) return;
    s.level += 1;
    s.open = {};
    s.touched = true;
    renderStructure(s, { keep: s.data.orgnr });
  });
  $("s-reset").addEventListener("click", () => {
    const s = mapState();
    if (!s) return;
    resetStructure(s);
    renderStructure(s);
    fitStructure(s, true);
  });
  $("s-zin").addEventListener("click", () => { const s = mapState(); if (s) zoomMid(s, 1.25); });
  $("s-zout").addEventListener("click", () => { const s = mapState(); if (s) zoomMid(s, 0.8); });
  $("s-fit").addEventListener("click", () => { const s = mapState(); if (s) fitStructure(s); });
  $("s-help").addEventListener("click", () => setMapHelp($("s-help-pop").hidden));
  document.addEventListener("click", (e) => {
    if (!$("s-help-pop").hidden && !$("s-help-pop").contains(e.target) && e.target !== $("s-help")) setMapHelp(false);
  });
  $("s-years").addEventListener("click", (e) => {
    const s = mapState();
    const y = e.target.closest("[data-s-year]");
    if (s && y) go("structure", { ...s.arg, year: Number(y.dataset.sYear) });
  });
  $("s-saved").addEventListener("click", async (e) => {
    const s = mapState();
    if (!s || e.target.id !== "s-redo") return;
    e.target.disabled = true;
    e.target.textContent = "Lager …";
    try {
      const d = await api("POST", structureUrl(s.arg, s.data.year));
      if (state.struct === s) setStructure(s, d);
    } catch (err) {
      setStatus(err.message || "Kunne ikke lage kartet på nytt.", true);
      e.target.disabled = false;
      e.target.textContent = "Lag på nytt";
    }
  });

  // Drag to pan, two fingers to zoom. The pointer is only captured once it has moved, so a click
  // on a name or a toggle still lands on it.
  const pointers = new Map();
  let drag = null, pinch = null;
  mapEl.addEventListener("pointerdown", (e) => {
    const s = mapState();
    if (!s || e.button > 0 || e.target.closest(".map-ctrl, .map-mini")) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    s.dragged = false;
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y), k: s.k };
      drag = null;
      pointers.forEach((_, id) => mapEl.setPointerCapture(id));
    } else {
      drag = { x: e.clientX, y: e.clientY, tx: s.tx, ty: s.ty };
    }
  });
  mapEl.addEventListener("pointermove", (e) => {
    const s = mapState();
    if (!s || !pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()], r = mapEl.getBoundingClientRect();
      s.touched = true;
      zoomAt(s, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top, (pinch.k * Math.hypot(a.x - b.x, a.y - b.y)) / pinch.dist / s.k);
      return;
    }
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!s.dragged) {
      if (Math.abs(dx) + Math.abs(dy) < 4) return;
      s.dragged = s.touched = true;
      mapEl.setPointerCapture(e.pointerId);
      mapEl.classList.add("drag");
    }
    s.tx = drag.tx + dx;
    s.ty = drag.ty + dy;
    applyView(s);
  });
  const endPointer = (e) => {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (pointers.size) return;
    drag = null;
    mapEl.classList.remove("drag");
    const s = state.struct;
    if (s) setTimeout(() => { s.dragged = false; }, 0);
  };
  mapEl.addEventListener("pointerup", endPointer);
  mapEl.addEventListener("pointercancel", endPointer);
  mapEl.addEventListener("wheel", (e) => {
    const s = mapState();
    if (!s) return;
    e.preventDefault();
    s.touched = true;
    const r = mapEl.getBoundingClientRect();
    if (e.ctrlKey || e.metaKey) zoomAt(s, e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : 0.0025)));
    else { s.tx -= e.deltaX; s.ty -= e.deltaY; applyView(s); }
  }, { passive: false });

  let miniDown = false;
  const miniTo = (e) => {
    const s = mapState();
    if (!s || !s.miniM) return;
    const r = miniEl.getBoundingClientRect();
    s.tx = mapEl.clientWidth / 2 - ((e.clientX - r.left) / s.miniM) * s.k;
    s.ty = mapEl.clientHeight / 2 - ((e.clientY - r.top) / s.miniM) * s.k;
    s.touched = true;
    applyView(s);
  };
  miniEl.addEventListener("pointerdown", (e) => { miniDown = true; miniEl.setPointerCapture(e.pointerId); miniTo(e); });
  miniEl.addEventListener("pointermove", (e) => { if (miniDown) miniTo(e); });
  miniEl.addEventListener("pointerup", () => { miniDown = false; });

  let mapResize = 0;
  window.addEventListener("resize", () => {
    clearTimeout(mapResize);
    mapResize = setTimeout(() => { const s = mapState(); if (s) renderStructure(s, { keep: s.data.orgnr }); }, 120);
  });
  // The mini map is drawn on a canvas, so it follows a theme change by hand.
  const redrawMini = () => { const s = mapState(); if (s) drawMiniMap(s); };
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redrawMini);
  new MutationObserver(redrawMini).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  function stopSubsPoll() {
    clearTimeout(state.subsTimer);
    state.subsTimer = null;
  }

  function pollSubs(orgnr) {
    stopSubsPoll();
    state.subsTimer = setTimeout(async () => {
      try {
        const s = await api("GET", `/api/company/${orgnr}/subsidiaries`);
        if (!state.data || state.data.company.orgnr !== orgnr || state.view !== "company") return;
        state.data.ownership.subsidiaries = s;
        renderSubs(s);
      } catch (e) {
        if (state.data && state.data.company.orgnr === orgnr) pollSubs(orgnr);
      }
    }, SUBS_POLL_MS);
  }

  async function startSubs(force) {
    const orgnr = state.data.company.orgnr;
    try {
      const s = await api("POST", `/api/company/${orgnr}/subsidiaries${force ? "?force=1" : ""}`);
      if (state.data.company.orgnr !== orgnr) return;
      state.data.ownership.subsidiaries = s;
      renderSubs(s);
    } catch (e) { setStatus(e.message, true); }
  }

  function renderSubs(s) {
    const orgnr = state.data.company.orgnr;
    const lastYear = state.data.years[state.data.years.length - 1];
    const year = s.year || lastYear;
    if (s.status === "running") {
      $("subs-src").textContent = year ? `Årsrapporten ${year}` : "";
      $("subs").innerHTML = '<p class="working"><span class="spinner"></span> Leser årsrapporten … Det tar gjerne under ett minutt.</p>';
      pollSubs(orgnr);
      return;
    }
    stopSubsPoll();
    if (s.status === "ok" && s.rows && s.rows.length) return renderSubsTable(s);
    $("subs-src").textContent = "Fra årsrapporten (PDF)";
    if (!lastYear) {
      $("subs").innerHTML = '<p class="note">Ingen årsrapport å lese datterselskaper fra.</p>';
    } else if (s.status === "none" || (s.status === "ok" && !(s.rows || []).length)) {
      $("subs").innerHTML = `<p class="note">Fant ingen liste over datterselskaper i årsrapporten ${esc(year)}.</p>
        <div class="sel-row"><button type="button" class="lk" data-subs="force">Les på nytt</button></div>`;
    } else if (s.status === "failed") {
      $("subs").innerHTML = `<p class="note danger">Kunne ikke lese årsrapporten: ${esc(s.error || "ukjent feil")}.</p>
        <div class="sel-row"><button type="button" class="secondary" data-subs="force">Prøv igjen</button></div>`;
    } else {
      $("subs").innerHTML = `<p class="note">Ingen datterselskaper lest inn ennå. Brreg har dem ikke som data, de står i notene i årsrapporten.</p>
        <div class="sel-row"><button type="button" class="secondary" data-subs="start">Les datterselskaper fra PDF ${esc(lastYear)}</button></div>`;
    }
  }

  function renderSubsTable(s) {
    const orgnr = state.data.company.orgnr;
    const pages = s.rows.map((r) => r.page).filter((p) => p);
    const page = pages.length ? Math.min(...pages) : null;
    $("subs-src").innerHTML = `Årsrapporten ${esc(s.year)} · lest av AI${page ? ` · <a href="/api/pdf/${esc(orgnr)}/${esc(s.year)}#page=${page}" target="_blank" rel="noopener">PDF s. ${page}</a>` : ""}`;
    if (!$("subs-q")) {
      $("subs").innerHTML = `<div class="subs-tools">
          <div class="search-box"><label for="subs-q" class="vh">Filtrer datterselskaper</label>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.8-4.8"/></svg>
            <input id="subs-q" type="search" autocomplete="off" placeholder="Filtrer, f.eks. «eiendom»"></div>
          <span class="muted num" id="subs-count"></span>
        </div>
        <div class="list-wrap"><table class="subs"><thead id="subs-head"></thead><tbody id="subs-rows"></tbody></table></div>
        <div class="sel-row" id="subs-foot"></div>`;
      $("subs-q").value = state.subsQuery;
    }
    // Many reports list only names (+ orgnr); drop columns no row has a value for.
    const placeOf = (r) => [r.place, r.country && !/^norge$/i.test(r.country) ? r.country : ""].filter(Boolean).join(", ");
    const hasPlace = s.rows.some((r) => placeOf(r));
    const hasShare = s.rows.some((r) => r.share_pct !== null && r.share_pct !== undefined);
    $("subs-head").innerHTML = `<tr><th>Selskap</th>${hasPlace ? "<th>Sted</th>" : ""}${hasShare ? "<th>Eierandel</th>" : ""}</tr>`;
    const q = state.subsQuery.trim().toLowerCase();
    const hits = s.rows.filter((r) => !q || r.name.toLowerCase().includes(q));
    const shown = q || state.subsAll ? hits : hits.slice(0, SUBS_PREVIEW);
    const mark = (n) => {
      if (!q) return esc(n);
      const i = n.toLowerCase().indexOf(q);
      return esc(n.slice(0, i)) + "<mark>" + esc(n.slice(i, i + q.length)) + "</mark>" + esc(n.slice(i + q.length));
    };
    $("subs-rows").innerHTML = shown.map((r) => {
      const name = r.orgnr ? `<button type="button" class="lk" data-open="${esc(r.orgnr)}">${mark(r.name)}</button>` : mark(r.name);
      const share = r.share_pct === null || r.share_pct === undefined ? "–" : `${esc(r.share_pct)} %`;
      return `<tr><td>${name}</td>${hasPlace ? `<td>${esc(placeOf(r))}</td>` : ""}${hasShare ? `<td class="num">${share}</td>` : ""}</tr>`;
    }).join("") || `<tr><td colspan="${1 + hasPlace + hasShare}" class="muted">Ingen treff</td></tr>`;
    $("subs-count").textContent = q ? `${hits.length} treff` : `${shown.length} av ${hits.length} vist`;
    $("subs-foot").innerHTML = (!q && !state.subsAll && hits.length > SUBS_PREVIEW ? `<button type="button" class="lk" id="subs-more">Vis alle ${hits.length}</button>` : "")
      + `<span class="right"><button type="button" class="lk muted" data-subs="force">Les på nytt</button></span>`;
  }

  $("subs").addEventListener("input", (e) => {
    if (e.target.id !== "subs-q") return;
    state.subsQuery = e.target.value;
    renderSubsTable(state.data.ownership.subsidiaries);
  });
  $("subs").addEventListener("click", (e) => {
    if (e.target.id === "subs-more") { state.subsAll = true; renderSubsTable(state.data.ownership.subsidiaries); return; }
    const b = e.target.closest("[data-subs]");
    if (!b) return;
    if (b.dataset.subs === "force") $("subs").innerHTML = "";
    startSubs(b.dataset.subs === "force");
  });

  // Collections popover on the company page
  function renderCollPop() {
    const member = new Set(state.data.collections || []);
    $("coll-pop-list").innerHTML = state.colls.length
      ? state.colls.map((c) => `<label><input type="checkbox" data-member="${c.id}"${member.has(c.id) ? " checked" : ""}> ${esc(c.name)} <span class="muted num">${c.count}</span></label>`).join("")
      : '<p class="note">Ingen samlinger ennå.</p>';
  }
  function closeCollPop() {
    $("coll-pop").hidden = true;
    $("coll-btn").setAttribute("aria-expanded", "false");
  }
  $("coll-btn").addEventListener("click", () => {
    const open = $("coll-pop").hidden;
    if (open) renderCollPop();
    $("coll-pop").hidden = !open;
    $("coll-btn").setAttribute("aria-expanded", String(open));
  });
  $("coll-pop-list").addEventListener("change", async (e) => {
    const id = Number(e.target.dataset.member);
    if (!id) return;
    const orgnr = state.data.company.orgnr;
    const on = e.target.checked;
    try {
      await api(on ? "PUT" : "DELETE", `/api/collections/${id}/members/${orgnr}`);
      const set = new Set(state.data.collections || []);
      if (on) set.add(id); else set.delete(id);
      state.data.collections = [...set];
      renderFavBtn();
      await refreshSide();
      renderCollPop();
    } catch (err) {
      e.target.checked = !on;
      setStatus(err.message, true);
    }
  });
  $("coll-pop-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("coll-pop-new").value.trim();
    if (!name) return;
    try {
      const res = await newCollection(name, [state.data.company.orgnr]);
      state.data.collections = [...(state.data.collections || []), res.id];
      $("coll-pop-new").value = "";
      renderFavBtn();
      renderCollPop();
      setStatus(`La ${state.data.company.name} i ny samling «${name}»`);
    } catch (err) { setStatus(err.message, true); }
  });

  // ---------- lists (favourites + one collection) ----------

  const RULES = [["latest", "Siste år"], ["last3", "Siste 3 år"], ["last5", "Siste 5 år"], ["all", "Alle år"]];

  function rulePayload(rule) {
    if (rule === "all") return { mode: "all" };
    return { mode: "last", last_n: { latest: 1, last3: 3, last5: 5 }[rule] };
  }

  function listHtml(items, mode, empty) {
    if (!items.length) return `<p class="empty">${empty}</p>`;
    const allOn = items.every((r) => state.picked.has(r.orgnr));
    const rows = items.map((r) => {
      const res = r.aarsresultat === null || r.aarsresultat === undefined
        ? '<span class="muted">–</span>'
        : `${short(r.aarsresultat, r.currency)}<small>${esc(r.key_year || "")}</small>`;
      const tail = mode === "favs"
        ? `<button type="button" class="sq" aria-pressed="true" data-unfav="${esc(r.orgnr)}" aria-label="Fjern ${esc(r.name)} fra favoritter"></button>`
        : `<button type="button" class="x" data-remove="${esc(r.orgnr)}" aria-label="Fjern ${esc(r.name)} fra samlingen">×</button>`;
      return `<tr>
        <td><input type="checkbox" data-pickrow="${esc(r.orgnr)}" aria-label="Velg ${esc(r.name)}"${state.picked.has(r.orgnr) ? " checked" : ""}></td>
        <td><button type="button" class="open" data-open="${esc(r.orgnr)}">${esc(r.name)}</button><small>${fmtOrgnr(r.orgnr)}${r.place ? " · " + esc(r.place) : ""}</small></td>
        <td class="r num">${res}</td>
        <td class="r num">${r.ek_andel === null || r.ek_andel === undefined ? '<span class="muted">–</span>' : pct(r.ek_andel)}</td>
        <td class="r">${r.last_pdf_year ? `<button type="button" class="lk num" data-pdf="${esc(r.orgnr)}" data-year="${esc(r.last_pdf_year)}">${esc(r.last_pdf_year)} PDF</button>` : '<span class="muted">–</span>'}</td>
        <td class="r">${tail}</td></tr>`;
    }).join("");
    const n = items.filter((r) => state.picked.has(r.orgnr)).length;
    return `<div class="list-wrap"><table class="list">
        <thead><tr>
          <th style="width:28px"><input type="checkbox" data-pickall aria-label="Velg alle"${allOn ? " checked" : ""}></th>
          <th>Selskap</th><th class="r">Årsresultat</th><th class="r">EK-andel</th><th class="r">Siste PDF</th><th style="width:32px"><span class="vh">Fjern</span></th>
        </tr></thead><tbody>${rows}</tbody></table></div>
      <div class="bulk">
        <label for="rule-${mode}" class="vh">Hvilke år</label>
        <select id="rule-${mode}" data-rule>${RULES.map(([v, t]) => `<option value="${v}"${state.rule === v ? " selected" : ""}>${t}</option>`).join("")}</select>
        <button type="button" class="primary" data-bulk${state.jobRunning ? " disabled" : ""}>Last ned · ${n ? `${n} valgt` : `alle ${items.length}`} (ZIP)</button>
      </div>`;
  }

  function bindList(root, getItems, label, rerender) {
    root.onclick = async (e) => {
      const t = e.target;
      const items = getItems();
      if (t.matches("[data-pickrow]")) { if (t.checked) state.picked.add(t.dataset.pickrow); else state.picked.delete(t.dataset.pickrow); rerender(); return; }
      if (t.matches("[data-pickall]")) { state.picked = t.checked ? new Set(items.map((r) => r.orgnr)) : new Set(); rerender(); return; }
      const unfav = t.closest("[data-unfav]");
      if (unfav) {
        try {
          await api("DELETE", `/api/favourites/${unfav.dataset.unfav}`);
          state.picked.delete(unfav.dataset.unfav);
          if (state.data && state.data.company.orgnr === unfav.dataset.unfav) state.data.favourite = false;
          await loadFavs();
          refreshSide();
        } catch (err) { setStatus(err.message, true); }
        return;
      }
      const rm = t.closest("[data-remove]");
      if (rm) {
        try {
          await api("DELETE", `/api/collections/${state.collId}/members/${rm.dataset.remove}`);
          state.picked.delete(rm.dataset.remove);
          await loadCollection(state.collId);
          refreshSide();
        } catch (err) { setStatus(err.message, true); }
        return;
      }
      if (t.closest("[data-bulk]")) {
        const chosen = items.filter((r) => state.picked.has(r.orgnr));
        const ids = (chosen.length ? chosen : items).map((r) => r.orgnr);
        startJob({ orgnrs: ids, ...rulePayload(state.rule), batch: true }, `${label}: ${ids.length} selskap${ids.length === 1 ? "" : "er"}`);
      }
    };
    root.onchange = (e) => { if (e.target.matches("[data-rule]")) { state.rule = e.target.value; rerender(); } };
  }

  async function loadFavs() {
    $("favs-body").innerHTML = '<p class="loading"><span class="spinner"></span> Henter favoritter …</p>';
    try {
      state.favRows = (await api("GET", "/api/favourites?rows=true")).items;
      state.favs = state.favRows.map(({ orgnr, name }) => ({ orgnr, name }));
      renderNav();
      renderFavs();
    } catch (e) { $("favs-body").innerHTML = `<p class="note danger">${esc(e.message)}</p>`; }
  }
  function renderFavs() {
    $("favs-body").innerHTML = listHtml(state.favRows, "favs", "Ingen favoritter ennå. Åpne et selskap og trykk «Legg til favoritt».");
  }
  bindList($("favs-body"), () => state.favRows, "Favoritter", renderFavs);

  async function loadCollections() {
    try {
      state.colls = (await api("GET", "/api/collections")).items;
      renderNav();
    } catch (e) { setStatus(e.message, true); }
    $("coll-cards").innerHTML = state.colls.map((c) =>
      `<button type="button" class="coll-card" data-coll="${c.id}"><strong>${esc(c.name)}</strong>
        <span>${c.count} selskap${c.count === 1 ? "" : "er"}</span>
        <span>${esc((c.preview || []).join(", "))}${c.count > (c.preview || []).length ? " …" : ""}</span></button>`).join("")
      || '<p class="empty">Ingen samlinger ennå. Lag en her, eller fra et selskap via «Legg i samling».</p>';
  }

  async function loadCollection(id) {
    state.collId = id;
    renderNav();
    try {
      state.coll = await api("GET", `/api/collections/${id}`);
    } catch (e) {
      if (e.status === 404) { go("collections"); setStatus("Fant ikke samlingen.", true); return; }
      setStatus(e.message, true);
      return;
    }
    if (state.collId !== id) return;
    renderCollection();
  }

  function renderCollection() {
    const c = state.coll;
    if (document.activeElement !== $("coll-name")) $("coll-name").value = c.name;
    $("coll-facts").textContent = `${c.items.length} selskap${c.items.length === 1 ? "" : "er"} · klikk navnet for å gi nytt navn`;
    $("coll-del-wrap").innerHTML = state.confirmDel
      ? `<span class="muted" style="font-size:14px">Slette «${esc(c.name)}»?</span><button type="button" class="secondary danger" id="del-yes">Slett</button><button type="button" class="secondary" id="del-no">Avbryt</button>`
      : '<button type="button" class="lk muted" id="del-coll" style="font-size:14px">Slett samling</button>';
    $("coll-body").innerHTML = listHtml(c.items, "coll", "Samlingen er tom. Legg til selskaper med feltet over, eller fra et selskap via «Legg i samling».");
    document.title = `${c.name} · Årsregnskap`;
  }
  bindList($("coll-body"), () => (state.coll ? state.coll.items : []), "Samling", renderCollection);

  let renameTimer = null;
  $("coll-name").addEventListener("input", (e) => {
    clearTimeout(renameTimer);
    const id = state.collId;
    const name = e.target.value.trim() || "Uten navn";
    renameTimer = setTimeout(async () => {
      try {
        await api("PATCH", `/api/collections/${id}`, { name });
        if (state.coll && state.coll.id === id) state.coll.name = name;
        refreshSide();
      } catch (err) { setStatus(err.message, true); }
    }, 400);
  });
  $("coll-name").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
  $("coll-del-wrap").addEventListener("click", async (e) => {
    if (e.target.id === "del-coll") { state.confirmDel = true; renderCollection(); return; }
    if (e.target.id === "del-no") { state.confirmDel = false; renderCollection(); return; }
    if (e.target.id === "del-yes") {
      const name = state.coll.name;
      try {
        await api("DELETE", `/api/collections/${state.collId}`);
        await refreshSide();
        go("collections");
        setStatus(`Slettet samlingen «${name}»`);
      } catch (err) { setStatus(err.message, true); }
    }
  });

  combobox({
    input: $("coll-q"), dropdown: $("coll-dropdown"), list: $("coll-results"), spinner: $("coll-spinner"),
    exclude: (orgnr) => !!(state.coll && state.coll.items.some((r) => r.orgnr === orgnr)),
    tag: () => "<span>Legg til</span>",
    onPick: async (c) => {
      $("coll-q").value = "";
      try {
        await api("PUT", `/api/collections/${state.collId}/members/${c.orgnr}`);
        await loadCollection(state.collId);
        refreshSide();
        $("coll-q").focus();
      } catch (err) { setStatus(err.message, true); }
    },
  });

  // ---------- batch ----------

  const ORGNR_RE = /(?<!\d)(\d{3}[\s ]?\d{3}[\s ]?\d{3})(?!\d)/g;
  const WEIGHTS = [3, 2, 7, 6, 5, 4, 3, 2];
  function validOrgnr(d) {
    const r = WEIGHTS.reduce((s, w, i) => s + w * Number(d[i]), 0) % 11;
    const check = (11 - r) % 11;
    return check !== 10 && check === Number(d[8]);
  }
  function parseBatch() {
    const ok = [];
    const bad = [];
    $("batch-input").value.split(/[\n\r,;]+/).map((t) => t.trim()).filter(Boolean).forEach((token) => {
      const found = token.match(ORGNR_RE);
      if (!found) { bad.push([token, "Ikke et organisasjonsnummer"]); return; }
      found.forEach((raw) => {
        const d = raw.replace(/\D/g, "");
        if (!validOrgnr(d)) bad.push([raw, "Feil kontrollsiffer"]);
        else if (!ok.includes(d)) ok.push(d);
      });
    });
    return { ok, bad };
  }
  function renderParsed() {
    const { ok, bad } = parseBatch();
    if (!ok.length && !bad.length) { $("parsed").innerHTML = '<p class="note">Maks 50 selskaper.</p>'; return; }
    const known = new Map(state.favs.map((f) => [f.orgnr, f.name]));
    const rows = ok.map((d) => `<li><span>${known.has(d) ? esc(known.get(d)) : '<span class="muted">Slås opp ved henting</span>'}</span><span class="num">${fmtOrgnr(d)}</span></li>`)
      .concat(bad.map(([raw, why]) => `<li class="bad"><span>${esc(raw)}</span><span>${why}</span></li>`));
    const over = ok.length > 50 ? ' · <span class="bad">over maks 50</span>' : "";
    $("parsed").innerHTML = `<b>${ok.length} gyldige</b>${bad.length ? ` · <span class="bad">${bad.length} ugyldige</span>` : ""}${over}<ul>${rows.join("")}</ul>`;
  }
  $("batch-input").addEventListener("input", renderParsed);
  $("use-favs").addEventListener("click", () => { $("batch-input").value = state.favs.map((f) => f.orgnr).join("\n"); renderParsed(); });
  $("clear-batch").addEventListener("click", () => { $("batch-input").value = ""; renderParsed(); });
  $("save-coll").addEventListener("click", async () => {
    const { ok } = parseBatch();
    if (!ok.length) { setStatus("Ingen gyldige organisasjonsnummer å lagre.", true); return; }
    try {
      const res = await newCollection("Innlimt liste", ok);
      go("collection", res.id);
      setStatus(`Lagret ${ok.length} selskaper som samling. Gi den et navn over.`);
    } catch (e) { setStatus(e.message, true); }
  });
  document.querySelectorAll("#batch-form input[type=number]").forEach((inp) =>
    inp.addEventListener("focus", () => { inp.closest("label").querySelector("input[type=radio]").checked = true; }));
  $("batch-form").addEventListener("submit", (e) => {
    e.preventDefault();
    setStatus("");
    const text = $("batch-input").value.trim();
    if (!text) { setStatus("Lim inn minst ett organisasjonsnummer.", true); return; }
    const mode = document.querySelector("input[name=year-mode]:checked").value;
    const payload = { orgnrs: text, batch: true };
    if (mode === "latest") Object.assign(payload, { mode: "last", last_n: 1 });
    else if (mode === "last") Object.assign(payload, { mode: "last", last_n: Number($("batch-last-n").value) || 1 });
    else if (mode === "all") payload.mode = "all";
    else {
      payload.mode = "range";
      payload.from_year = Number($("batch-from").value) || null;
      payload.to_year = Number($("batch-to").value) || null;
      if (!payload.from_year || !payload.to_year) { setStatus("Fyll ut både fra- og til-år.", true); return; }
    }
    startJob(payload, "Årsregnskap for flere selskaper");
  });
  const thisYear = new Date().getFullYear();
  $("batch-from").value = thisYear - 5;
  $("batch-to").value = thisYear - 1;

  // ---------- jobs (ZIP) ----------

  function setJobRunning(on) {
    state.jobRunning = on;
    $("batch-btn").disabled = on;
    document.querySelectorAll("[data-bulk]").forEach((b) => { b.disabled = on; });
    if (state.data && state.view === "company" && !$("dl-pop").hidden) renderDlMenu();
  }

  async function startJob(payload, title) {
    clearTimeout(state.jobTimer);
    setStatus("");
    $("job-title").textContent = title;
    $("job-text").textContent = "Starter …";
    $("job-count").textContent = "";
    $("job-bar-fill").style.width = "0%";
    $("job-bar").classList.add("indeterminate");
    $("job-done").hidden = true;
    $("job-failures").hidden = true;
    $("job-companies").hidden = true;
    $("job").hidden = false;
    $("job").scrollIntoView({ behavior: "smooth", block: "nearest" });
    setJobRunning(true);
    try {
      const job = await api("POST", "/api/jobs", payload);
      pollJob(job.id, payload.batch);
    } catch (e) {
      $("job").hidden = true;
      setJobRunning(false);
      setStatus(e.message || "Kunne ikke starte nedlastingen.", true);
    }
  }

  async function pollJob(id, batch) {
    let d;
    try {
      d = await api("GET", `/api/jobs/${id}`);
    } catch (e) {
      $("job-text").textContent = e.message || "Mistet kontakten med serveren.";
      setJobRunning(false);
      return;
    }
    renderJob(d, batch);
    if (d.phase === "resolving" || d.phase === "downloading") {
      state.jobTimer = setTimeout(() => pollJob(id, batch), JOB_POLL_MS);
      return;
    }
    setJobRunning(false);
    // Deferred: the download click below would cancel a smooth scroll started before it.
    setTimeout(() => $("job").scrollIntoView({ behavior: "smooth", block: "nearest" }), 100);
    if (d.phase === "done") {
      const href = `/api/jobs/${id}/download`;
      $("job-download").setAttribute("href", href);
      $("job-done").hidden = false;
      triggerDownload(href, d.filename);
    }
  }

  function renderJob(d, batch) {
    const bar = $("job-bar");
    const fill = $("job-bar-fill");
    if (d.phase === "resolving") {
      bar.classList.add("indeterminate");
      $("job-text").textContent = d.companies_total > 1 ? `Slår opp selskaper … ${d.companies_done} av ${d.companies_total}` : "Slår opp selskapet …";
      $("job-count").textContent = "";
    } else if (d.phase === "downloading") {
      bar.classList.remove("indeterminate");
      fill.style.width = `${d.total ? Math.round((100 * d.done) / d.total) : 0}%`;
      $("job-text").textContent = "Henter PDF-er fra Brreg …";
      $("job-count").textContent = `${d.done} / ${d.total}`;
    } else if (d.phase === "done") {
      bar.classList.remove("indeterminate");
      fill.style.width = "100%";
      const partial = d.failures.length ? ` ${d.failures.length} mangler, se under.` : "";
      $("job-text").textContent = `Ferdig: ${d.ok_files} PDF-er i ${d.filename}.${partial}`;
      $("job-count").textContent = `${d.ok_files} / ${d.total}`;
    } else {
      bar.classList.remove("indeterminate");
      fill.style.width = "0%";
      $("job-text").textContent = d.error || "Nedlastingen feilet.";
      $("job-count").textContent = "";
    }
    const failures = d.failures || [];
    $("job-failures").hidden = !failures.length;
    $("job-failure-list").innerHTML = failures.map((f) => {
      const who = f.name ? `${esc(f.name)} (${fmtOrgnr(f.orgnr)})` : (/^\d{9}$/.test(f.orgnr) ? fmtOrgnr(f.orgnr) : `«${esc(f.orgnr)}»`);
      return `<li><span>${who}${f.year ? " " + esc(f.year) : ""}</span><span class="why">${esc(f.reason)}</span></li>`;
    }).join("");
    const showCompanies = batch && d.companies && d.companies.length && d.phase !== "resolving";
    $("job-companies").hidden = !showCompanies;
    if (showCompanies) {
      $("job-company-list").innerHTML = d.companies.map((c) => {
        const status = d.phase === "done" || d.phase === "failed" ? `${c.ok_years.length} / ${c.years.length} år` : `${c.years.length} år`;
        return `<li><span>${esc(c.name)} <span class="muted num">${fmtOrgnr(c.orgnr)}</span></span><span>${status}</span></li>`;
      }).join("");
    }
  }

  // ---------- chat ----------

  const narrow = window.matchMedia("(max-width: 1240px)");
  function setChat(open) {
    $("chat").hidden = !open;
    $("app").classList.toggle("chat-closed", !open);
    $("chat-toggle").setAttribute("aria-expanded", String(open));
    if (!narrow.matches) storeSet("regnskap:chat-open", open ? "1" : "0");
  }
  $("chat-toggle").addEventListener("click", () => setChat($("chat").hidden));
  $("chat-close").addEventListener("click", () => setChat(false));
  narrow.addEventListener("change", (e) => setChat(!e.matches && storeGet("regnskap:chat-open", "1") === "1"));
  setChat(!narrow.matches && storeGet("regnskap:chat-open", "1") === "1");

  function mdLite(text) {
    const lines = esc(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").split("\n");
    let html = "";
    let inList = false;
    let para = [];
    const flush = () => { if (para.length) { html += `<p>${para.join("<br>")}</p>`; para = []; } };
    for (const line of lines) {
      const m = line.match(/^\s*[-*•]\s+(.*)$/);
      if (m) { flush(); if (!inList) { html += "<ul>"; inList = true; } html += `<li>${m[1]}</li>`; continue; }
      if (inList) { html += "</ul>"; inList = false; }
      if (!line.trim()) flush(); else para.push(line.replace(/^#+\s*/, ""));
    }
    flush();
    if (inList) html += "</ul>";
    return html;
  }

  function suggestions() {
    if (state.view === "company" && state.data) {
      const kf = state.data.key_figures;
      const out = [];
      if (kf) {
        out.push(`Oppsummer ${kf.year} i tre punkter`);
        out.push(kf.driftsresultat !== null && kf.driftsresultat < 0 ? "Hvorfor er driftsresultatet negativt?" : "Hva driver resultatet?");
        if (kf.ek_andel !== null && kf.ek_andel !== undefined) out.push(`Hva betyr EK-andel på ${pct(kf.ek_andel)}?`);
      } else {
        out.push("Hvorfor finnes ingen nøkkeltall her?");
      }
      out.push("Hvem sitter i styret, og hvem er revisor?");
      return out.slice(0, 3);
    }
    if (state.view === "structure" && state.struct && state.struct.arg.person) return ["Hvilke selskaper eier personen mest av?", "Hvor stort er det personen eier i alt?"];
    if (state.view === "structure") return ["Hvem eier selskapet til syvende og sist?", "Hva er de største delene av konsernet?"];
    if (state.view === "favs") return ["Hvem av favorittene har høyest EK-andel?", "Hvem hadde best årsresultat?"];
    if (state.view === "collection") return ["Sammenlign selskapene i samlingen", "Hvem har lavest EK-andel?"];
    return ["Hva kan jeg spørre om her?"];
  }

  function renderChatContext() {
    let ctx = "Ser på <b>verktøyet</b>";
    if (state.view === "company" && state.data) ctx = `Ser på <b>${esc(state.data.company.name)}</b>`;
    else if (state.view === "structure" && state.struct && state.struct.data) ctx = `Ser på ${state.struct.arg.person ? "eierstrukturen" : "selskapsstrukturen"} for <b>${esc(state.struct.data.name)}</b>`;
    else if (state.view === "collection" && state.coll) ctx = `Ser på samlingen <b>${esc(state.coll.name)}</b>`;
    else if (state.view === "favs") ctx = "Ser på <b>Favoritter</b>";
    $("chat-ctx").innerHTML = ctx;
    $("sugg").innerHTML = state.chatBusy ? "" : suggestions().map((s) => `<button type="button" data-ask="${esc(s)}">${esc(s)}</button>`).join("");
    if (!$("msgs").children.length) {
      $("msgs").innerHTML = '<div class="chat-empty"><p>Still spørsmål om tallene, rollene og eierskapet for selskapet du ser på, eller om favorittene og samlingene dine.</p><p>Tastatur: K åpner chatten.</p></div>';
    }
  }
  $("sugg").addEventListener("click", (e) => { const b = e.target.closest("[data-ask]"); if (b) ask(b.dataset.ask); });

  function addMsg(cls, html) {
    const empty = $("msgs").querySelector(".chat-empty");
    if (empty) empty.remove();
    const node = document.createElement("div");
    node.className = `msg ${cls}`;
    node.innerHTML = html;
    $("msgs").appendChild(node);
    $("msgs").scrollTop = $("msgs").scrollHeight;
    return node;
  }

  async function ask(text) {
    text = text.trim();
    if (!text || state.chatBusy) return;
    addMsg("user", esc(text));
    state.turns.push({ role: "user", content: text });
    $("chat-in").value = "";
    $("chat-in").style.height = "auto";
    const bot = addMsg("bot", '<span class="who">AI</span><div class="body muted">Tenker …</div>');
    const body = bot.querySelector(".body");
    state.chatBusy = true;
    $("send").disabled = true;
    renderChatContext();
    const payload = { messages: state.turns.slice(-12), view: state.view };
    if (state.view === "company" && state.data) payload.orgnr = state.data.company.orgnr;
    if (state.view === "structure" && state.struct && state.struct.orgnr) payload.orgnr = state.struct.orgnr;
    if (state.view === "collection" && state.collId) payload.collection_id = state.collId;
    try {
      const res = await api("POST", "/api/chat", payload);
      body.className = "body";
      body.innerHTML = mdLite(res.text || "");
      state.turns.push({ role: "assistant", content: res.text || "" });
    } catch (e) {
      state.turns.pop();
      bot.classList.add("err");
      body.className = "body";
      body.textContent = e.message || "Fikk ikke svar. Prøv igjen.";
    } finally {
      state.chatBusy = false;
      $("send").disabled = false;
      $("msgs").scrollTop = $("msgs").scrollHeight;
      renderChatContext();
    }
  }

  $("composer").addEventListener("submit", (e) => { e.preventDefault(); ask($("chat-in").value); });
  $("chat-in").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
  });
  $("chat-in").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 140) + "px"; });
  $("chat-new").addEventListener("click", () => {
    state.turns = [];
    $("msgs").innerHTML = "";
    renderChatContext();
  });

  // ---------- boot ----------

  refreshSide();
  route();
})();

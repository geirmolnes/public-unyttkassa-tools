// Eierkart layout: the saved structure (GET /api/company/{orgnr}/structure) as a sideways org
// chart. Owners to the left, holdings to the right. The API lists each company's owners and
// holdings once; the layout unfolds that into a tree, drawing a company with several parents
// whole under each of them. Only a loop (A owns B owns … A) stops, marked «går i ring». A long
// sibling list becomes a block of one-line tiles, alphabetical, with the part of the name they
// all share said once. A stake is written on its line, between owner and owned, not in a box.
//
// Open state is per place, not per company: a place's id is the keys on the way there from the
// company, joined by ">" (holdings) or "<" (owners).
//
// No DOM here: app.js passes the window size, a text measurer and the name links, and draws the
// result. tests/js/eierkart.test.js runs it under bun.
import { esc, fmtInt, fmtPct, norwegian, plural } from "./format.js";

// Box, gaps and block sizes in map pixels. A stake is drawn on its line, so the column gap CG
// leaves each edge's own stretch (CG − TRUNK = 76 px, a bracket sitting TRUNK px from its one
// side) room for the widest label, «< 0,01 %» (~54 px with its padding), with line either side.
export const MAP = { BW: 210, BH: 48, CG: 88, TRUNK: 12, VG: 10, GL: 18, PAD: 32, TH: 24, TG: 16, BPAD: 10, BHEAD: 30, BTOP: 5,
                     BLOCK_MIN: 11, BRANCH_MAX: 6 };
// Boxes a side may open by itself (owners all the way up, holdings to the chosen level). Past
// this, places stay closed until clicked. In the 2025 register the biggest owner tree unfolds to
// 1166 boxes (SENJA DEST AS); of the 400 biggest groups, NORGESGRUPPEN ASA's holdings to 820.
export const MAX_BOXES = 2000;
// Below this zoom the whole map is too small to read, so it opens on the company instead.
export const FIT_MIN = 0.55;
const OPEN_ZOOM = 0.85;
const nameOrder = new Intl.Collator("nb", { sensitivity: "base", numeric: true });

/** A stake as a label on the line between owner and owned; muted for a stake under the line and
 * for ownership that goes in a ring. The mini map on the company page uses it too. */
export const stakeLabel = (pct, muted = false, style = "") =>
  `<span class="ep num${muted ? " soft" : ""}"${style ? ` style="${style}"` : ""}>${fmtPct(pct)}</span>`;

/** First parent and depth of every node below (or above) `from`, breadth first: search opens the
 * shortest way to a hit. A stake under the line gets the first parent that lists it; with
 * `follow`, one whose holdings are known (followed, or opened elsewhere) opens like any. */
export function treeIndex(edges, from, follow = false) {
  const parent = {}, depth = { [from]: 0 };
  const queue = [from];
  for (const of of queue) {
    for (const [key, , kind] of edges[of] || []) {
      if (key in depth) continue;
      if (kind === "own" || (follow && kind === "small" && key in edges)) {
        parent[key] = of; depth[key] = depth[of] + 1; queue.push(key);
      }
      else if (!(key in parent)) parent[key] = of;
    }
  }
  return { parent, depth };
}

/** Place ids from the company down to `key`'s parent, along `parent` (a treeIndex parent). */
export function pathIds(parent, key, sep) {
  const keys = [];
  for (let p = parent[key]; p !== undefined; p = parent[p]) keys.unshift(p);
  let id = null;
  return keys.map((k) => (id = id === null ? k : `${id}${sep}${k}`));
}

/** Add what a stake under the line holds, from GET /holdings-tree, to the structure `d`.
 * Companies the map already has keep what it has for them. */
export function mergeFollow(d, sub) {
  for (const [of, list] of Object.entries(sub.edges)) {
    if (!(of in d.down)) { d.down[of] = list; d.under[of] = sub.under[of]; }
  }
  for (const [c, n] of Object.entries(sub.under)) if (!(c in d.under)) d.under[c] = n;
  for (const [c, name] of Object.entries(sub.names)) if (!d.nodes[c]) d.nodes[c] = { name, orgnr: c };
  d.more = { ...(d.more || {}), ...sub.more };
}

/** Shared whole-word prefix (and, in a block, suffix) of sibling names, leaving each at least a word. */
export function nameParts(names, withSuffix) {
  const ws = names.map((n) => (n || "").split(" "));
  let pre = 0, suf = 0;
  if (ws.length >= 2) {
    const min = Math.min(...ws.map((w) => w.length));
    while (pre < min - 1 && ws.every((w) => w[pre] === ws[0][pre])) pre++;
    if (withSuffix) while (pre + suf < min - 1 && ws.every((w) => w[w.length - 1 - suf] === ws[0][ws[0].length - 1 - suf])) suf++;
  }
  return { pre, suf, prefix: ws[0].slice(0, pre).join(" "), suffix: suf ? ws[0].slice(ws[0].length - suf).join(" ") : "" };
}
export const stripName = (name, pre, suf) => { const w = (name || "").split(" "); return w.slice(pre, w.length - suf).join(" ") || name; };
export const initialOf = (t) => { const c = (t || "").trim().charAt(0).toLocaleUpperCase("nb"); return /\d/.test(c) ? "#" : c; };

/**
 * Lay out the map for `view` ({data, open, upOpen, level, small, q, parent, upParent, extra},
 * and optionally {follow}: stakes under the line can be opened on request). `open` / `upOpen`
 * map place ids to what was clicked; other places open down to `level` (default 1) and all the
 * way up, MAX_BOXES per side. `env` = {width, height} of the map's frame,
 * `measure(text, condensed, weight)` in px at 12 px, and `nameHtml(node, text)` for a name.
 *
 * Returns {html, lines, blocks, rects, pos, at, spots, kidsBox, W, H, hits, cand,
 * capped}: `rects` are everything drawn ({x, y, w, h, type}), `pos[key]` the first place a
 * company or person is drawn, `spots[key]` every place, `at[id]` and `kidsBox[id]` one place
 * and what it opens, `hits` the search hits in reading order, `capped` whether MAX_BOXES kept
 * places closed.
 */
export function layoutMap(view, env) {
  view.extra = view.extra || {};
  let L = layoutPass(view, env);
  // A block's columns depend on how tall the rest of the map is beside it, known after a pass;
  // one more pass when that moved.
  let again = false;
  for (const b of L.blocks) {
    const extra = Math.max(0, L.H - 2 * MAP.PAD - b.h);
    if (Math.abs(extra - (view.extra[b.key] || 0)) > 24) { view.extra[b.key] = extra; again = true; }
  }
  if (again) L = layoutPass(view, env);
  return L;
}

/** Zoom and offset that show the whole map (W × H) in a vw × vh frame. On `opening`, a map that
 * would be smaller than FIT_MIN shows `root` (its rect) at a readable size instead. */
export function fitView(W, H, vw, vh, root, opening = false) {
  const m = 24, mt = 40;
  const k = Math.min((vw - 2 * m) / W, (vh - mt - m) / H, 1);
  if (opening && k < FIT_MIN && root) {
    return { k: OPEN_ZOOM, tx: vw / 2 - (root.x + root.w / 2) * OPEN_ZOOM, ty: vh / 2 - (root.y + root.h / 2) * OPEN_ZOOM };
  }
  return { k, tx: (vw - W * k) / 2, ty: mt + (vh - mt - m - H * k) / 2 };
}

/** Keep at least `margin` px of the map inside the frame. */
export function clampView({ k, tx, ty }, W, H, vw, vh, margin = 80) {
  return {
    tx: Math.min(vw - margin, Math.max(margin - W * k, tx)),
    ty: Math.min(vh - margin, Math.max(margin - H * k, ty)),
  };
}

// One layout pass; layoutMap runs it once or twice.
function layoutPass(view, env) {
  const d = view.data;
  const { BW, BH, CG, TRUNK, VG, GL, PAD, TH, TG, BPAD, BHEAD, BTOP, BLOCK_MIN, BRANCH_MAX } = MAP;
  const node = (key) => d.nodes[key] || { name: key };
  const isPerson = (n) => !n.orgnr && !!n.birth_year;
  const isForeign = (n) => (n.orgnr && !norwegian(n.orgnr)) || (n.country && n.country !== "NO");
  const shown = (key) => { const e = d.down[key] || []; return view.small ? e : e.filter((x) => x[2] !== "small"); };
  const open = { ...view.open }, upOpen = { ...view.upOpen };
  const level = view.level ?? 1;
  // A stake under the line that holds something the map doesn't have yet: it gets a button.
  const followable = (key) => !!view.follow && !(key in d.down) && (d.more || {})[key] > 0;
  const followBtn = (key, id, short) => {
    const n = d.more[key];
    return `<button type="button" class="mt" data-follow="${esc(key)}" data-mid="${esc(id)}" title="Følg: ${esc(plural(n, "eierandel", "eierandeler"))}">▸ ${short ? fmtInt(n) : plural(n, "eierandel", "eierandeler")}</button>`;
  };
  const q = view.q.trim().toLocaleUpperCase("nb");
  const cand = new Set();
  if (q.length >= 2) {
    Object.keys(d.nodes).forEach((key) => {
      if (key === d.orgnr || !(d.nodes[key].name || "").toLocaleUpperCase("nb").includes(q)) return;
      cand.add(key);
      pathIds(view.parent, key, ">").forEach((id) => { open[id] = true; });
      pathIds(view.upParent, key, "<").forEach((id) => { upOpen[id] = true; });
    });
  }
  // Boxes left on each side before places stop opening by themselves (clicked ones still do).
  const room = { down: MAX_BOXES, up: MAX_BOXES };
  let capped = false;
  const opensHere = (n, state, side, byDefault, count) => {
    const asked = n.kind === "root" ? true : n.id in state ? state[n.id] : null;
    if (asked === false || (asked === null && !byDefault)) return false;
    if (asked === null && room[side] <= 0) { capped = true; return false; }
    room[side] -= count;
    return true;
  };

  // ---- Holdings (right) ----
  const buildDown = (key, kind, pct, depth, id, anc) => {
    const opens = kind === "own" || kind === "root" || (kind === "small" && !!view.follow && key in d.down);
    const n = { key, id, kind: opens && anc.has(key) ? "cycle" : kind, pct, depth, side: 1, kids: [], block: null };
    const list = opens && n.kind !== "cycle" ? shown(key) : [];
    n.has = list.length > 0;
    n.nVis = list.length;
    n.open = n.has && opensHere(n, open, "down", depth < level, list.length);
    if (!n.open) return n;
    const inner = new Set(anc).add(key);
    const kids = list.map(([k, p, kd]) => buildDown(k, kd, p, depth + 1, `${id}>${k}`, inner));
    const dense = kids.length >= BLOCK_MIN;
    const g = nameParts(kids.map((k) => node(k.key).name), dense);
    let boxes = kids, tiles = [];
    if (dense) {
      // Companies with companies under them stay boxes above the block, so they can open.
      boxes = [];
      let branches = 0;
      for (const c of kids) {
        if (c.open) boxes.push(c);
        else if (c.kind === "own" && (d.under[c.key] || 0) > 0 && branches < BRANCH_MAX) { boxes.push(c); branches++; }
        else tiles.push(c);
      }
      if (tiles.length < 4) { boxes = kids; tiles = []; }
    }
    boxes.forEach((c) => { c.disp = stripName(node(c.key).name, g.pre, 0); });
    n.kids = boxes;
    if (tiles.length) n.block = makeBlock(tiles, g, depth + 1, key);
    // Each box's stake is on its own line, so only a shared name prefix needs saying above them.
    if (boxes.length && g.prefix) n.label = `<b>${esc(g.prefix)} …</b>`;
    return n;
  };
  const makeBlock = (tiles, g, col, key) => {
    tiles.forEach((t) => { t.disp = stripName(node(t.key).name, g.pre, g.suf); });
    tiles.sort((a, b) => nameOrder.compare(a.disp, b.disp));
    const pcts = new Set(tiles.map((t) => t.pct));
    const uniform = pcts.size === 1 ? tiles[0].pct : null;
    const widths = tiles.map((t) => env.measure(t.disp, true)).sort((a, b) => a - b);
    const p95 = widths[Math.min(widths.length - 1, Math.floor(widths.length * 0.95))];
    const nameW = Math.max(110, Math.min(230, Math.ceil(p95) + 2));
    const anyToggle = tiles.some((t) => t.has || (t.kind === "small" && followable(t.key)));
    const pattern = g.prefix || g.suffix ? `${g.prefix ? `${g.prefix} ` : ""}…${g.suffix ? ` ${g.suffix}` : ""}` : "";
    const first = initialOf(tiles[0].disp), last = initialOf(tiles[tiles.length - 1].disp);
    const meta = [plural(tiles.length, "selskap", "selskaper"), uniform !== null && `alle ${fmtPct(uniform)}`, first !== last && `${first}–${last}`].filter(Boolean).join(" · ");
    const headW = 20 + (pattern ? env.measure(pattern, false, 800) + 8 : 0) + env.measure(meta, false, 400);
    return { key, tiles, col, uniform, pattern, meta, headW, tw: 20 + nameW + (uniform === null ? 46 : 0) + (anyToggle ? 40 : 0) + 6 };
  };
  // Columns that make the whole map fit the window at the largest zoom. `extra` is the height
  // the rest of the map adds beside the block, known after a first pass (renderStructure).
  const sizeBlock = (b, xOff) => {
    const n = b.tiles.length, vw = Math.max(320, env.width), vh = Math.max(240, env.height);
    const extra = view.extra[b.key] || 0;
    let best = null;
    for (let cols = 1; cols <= Math.max(1, Math.min(24, Math.ceil(n / 4))); cols++) {
      const rows = Math.ceil(n / cols);
      const w = Math.max(b.headW, 2 * BPAD + cols * b.tw + (cols - 1) * TG);
      const h = BHEAD + BTOP + rows * TH + BPAD;
      const k = Math.min(vw / (xOff + w + PAD), vh / (h + extra + 2 * PAD));
      if (!best || k > best.k + 1e-6) best = { cols, rows, w, h, k };
    }
    Object.assign(b, best);
    b.a = b.h / 2;
  };
  const measureDown = (n, rootX) => {
    if (!n.open) { n.h = BH; n.a = BH / 2; return; }
    n.kids.forEach((k) => measureDown(k, rootX));
    const items = [...n.kids];
    if (n.block) { sizeBlock(n.block, rootX + n.block.col * (BW + CG)); items.push(n.block); }
    let y = n.label ? GL : 0;
    for (const it of items) { it.y0 = y; y += it.h + VG; }
    const total = y - VG;
    const a = Math.max(BH / 2, (items[0].y0 + items[0].a + items[items.length - 1].y0 + items[items.length - 1].a) / 2);
    n.a = a;
    n.h = Math.max(total, a + BH / 2);
  };

  // ---- Owners (left) ----
  const buildUp = (key, kind, pct, id, anc) => {
    const n = { key, id, kind: kind === "own" && anc.has(key) ? "cycle" : kind, pct, side: -1, kids: [] };
    const edges = n.kind === "own" || n.kind === "root" ? d.up[key] || [] : [];
    n.has = edges.length > 0;
    n.nOwners = edges.length;
    n.open = n.has && opensHere(n, upOpen, "up", true, edges.length);
    if (!n.open) return n;
    const inner = new Set(anc).add(key);
    n.kids = edges.map(([k, p, kd]) => buildUp(k, kd, p, `${id}<${k}`, inner));
    return n;
  };
  const upCols = (n) => 1 + (n.kids.length ? Math.max(...n.kids.map(upCols)) : 0);
  const measureUp = (n) => {
    if (!n.kids.length) { n.top = 0; n.h = BH; n.a = BH / 2; return; }
    n.kids.forEach(measureUp);
    let y = 0;
    for (const k of n.kids) { k.y0 = y; y += k.h + VG; }
    const total = y - VG;
    const mid = (n.kids[0].y0 + n.kids[0].a + n.kids[n.kids.length - 1].y0 + n.kids[n.kids.length - 1].a) / 2;
    let top = mid - BH / 2, shift = 0;
    if (top < 0) { shift = -top; top = 0; }
    n.kids.forEach((k) => { k.y0 += shift; });
    n.top = top;
    n.h = Math.max(total + shift, top + BH);
    n.a = top + BH / 2;
  };

  // ---- HTML ----
  const toggle = (n) => {
    let count;
    if (n.side < 0) count = plural(n.nOwners, "eier", "eiere");
    else if ((d.under[n.key] || 0) > 0) count = `${fmtInt(d.under[n.key])} under`;
    else count = plural(n.nVis, "eierandel", "eierandeler");
    return `<button type="button" class="mt" data-mkey="${esc(n.id)}" data-mside="${n.side}" aria-expanded="${n.open}">${n.open ? "▾" : "▸"} ${count}</button>`;
  };
  const chipFor = (n, nd) => {
    if (n.kind === "cycle") return "går i ring";
    if (n.has) return toggle(n);
    if (n.kind === "small") return followable(n.key) ? followBtn(n.key, n.id) : `${esc(d.rules.below)}, ikke fulgt`;
    if (isPerson(nd)) return `f. ${nd.birth_year}`;
    if (isForeign(nd)) return "utenlandsk";
    return "";
  };
  const cycleTip = (nd) => `Eierskapet går i ring: ${nd.name} står allerede i denne linjen.`;
  const boxHtml = (n, x, y) => {
    const nd = node(n.key);
    const cls = ["mb", isPerson(nd) && "person", (n.kind === "cycle" || n.kind === "small") && "dashed", cand.has(n.key) && "hit"].filter(Boolean).join(" ");
    const chip = chipFor(n, nd);
    const tip = n.kind === "cycle" ? ` title="${esc(cycleTip(nd))}"` : "";
    // The stake is on the line, not in the box. No chip: leave the top line out so the name gets both lines.
    const l1 = chip ? `<span class="l1"><span class="chip">${chip}</span></span>` : "";
    return `<div class="${cls}" data-k="${esc(n.key)}" style="left:${x}px;top:${y}px"${tip}>${l1}
      <span class="l2">${env.nameHtml(nd, n.disp || nd.name)}</span></div>`;
  };
  const rootHtml = (x, y) => {
    const sub = `${fmtInt((d.down[d.orgnr] || []).length)} direkte · ${fmtInt(d.stats.companies)} i alt`;
    return `<div class="mb root" data-k="${esc(d.orgnr)}" style="left:${x}px;top:${y}px"><b title="${esc(d.name)}">${esc(d.name || d.orgnr)}</b><span>${sub}</span></div>`;
  };

  // ---- Placement ----
  const out = { html: [], lines: [], blocks: [], rects: [], pos: {}, at: {}, spots: {}, kidsBox: {} };
  const spot = (n, r) => {
    out.at[n.id] = r;
    (out.spots[n.key] = out.spots[n.key] || []).push(r);
    if (!(n.key in out.pos)) out.pos[n.key] = r;
  };
  const addPos = (n, x, y, w, h, type) => {
    out.rects.push({ x, y, w, h, type });
    spot(n, { x, y, w, h });
  };
  const P = (v) => Math.round(v) + 0.5; // crisp 1px lines
  // k's stake centred on its own stretch of line, x1..x2 at height y.
  const stake = (x1, x2, y, k) => {
    out.html.push(stakeLabel(k.pct, k.kind === "small" || k.kind === "cycle", `left:${(x1 + x2) / 2}px;top:${P(y)}px`));
  };
  // Owners of the box at x (its line at py). Each owner's stretch runs to the bracket, or over the
  // whole gap when a single owner sits level with the box.
  const upLines = (x, py, ends, kids) => {
    const mx = x - TRUNK, kx = x - CG;
    const ys = ends.concat(py);
    out.lines.push(`M${P(x)} ${P(py)}H${P(mx)}M${P(mx)} ${P(Math.min(...ys))}V${P(Math.max(...ys))}` + ends.map((ay) => `M${P(mx)} ${P(ay)}H${P(kx)}`).join(""));
    const level = ends.length === 1 && Math.abs(ends[0] - py) < 1;
    kids.forEach((k, i) => stake(kx, level ? x : mx, ends[i], k));
  };
  const blockHtml = (b) => {
    out.rects.push({ x: b.x, y: b.y, w: b.w, h: b.h, type: "block" });
    const parts = [`<div class="bh">${b.pattern ? `<b>${esc(b.pattern)}</b>` : ""}<span>${esc(b.meta)}</span></div>`];
    for (let c = 1; c < b.cols; c++) {
      parts.push(`<i class="csep" style="left:${BPAD + c * (b.tw + TG) - TG / 2}px;top:${BHEAD + BTOP}px;height:${Math.min(b.rows, b.tiles.length - c * b.rows) * TH}px"></i>`);
    }
    b.tiles.forEach((t, i) => {
      const col = Math.floor(i / b.rows), row = i % b.rows;
      const lx = BPAD + col * (b.tw + TG), ly = BHEAD + BTOP + row * TH;
      const ini = initialOf(t.disp), prev = i ? initialOf(b.tiles[i - 1].disp) : null;
      const mark = ini !== prev ? `<span class="ini">${esc(ini)}</span>` : row === 0 ? `<span class="ini cont">${esc(ini)}</span>` : '<span class="ini"></span>';
      const nd = node(t.key);
      const cls = ["tile", isPerson(nd) && "person", (t.kind === "cycle" || t.kind === "small") && "dashed", cand.has(t.key) && "hit"].filter(Boolean).join(" ");
      const pct = b.uniform === null ? `<span class="tp">${fmtPct(t.pct)}</span>` : "";
      const tip = [nd.name, fmtPct(t.pct), t.kind === "cycle" ? cycleTip(nd) : t.kind === "small" ? `${d.rules.below}, ikke fulgt` : ""].filter(Boolean).join(" · ");
      let tg = "";
      if (t.has) {
        const cnt = (d.under[t.key] || 0) > 0 ? `${fmtInt(d.under[t.key])} under` : plural(t.nVis, "eierandel", "eierandeler");
        tg = `<button type="button" class="mt" data-mkey="${esc(t.id)}" data-mside="1" aria-expanded="false" title="Åpne: ${esc(cnt)}">▸ ${fmtInt(d.under[t.key] || t.nVis)}</button>`;
      } else if (t.kind === "small" && followable(t.key)) {
        tg = followBtn(t.key, t.id, true);
      }
      parts.push(`<div class="${cls}" data-k="${esc(t.key)}" style="left:${lx}px;top:${ly}px;width:${b.tw}px" title="${esc(tip)}">${mark}<span class="tn">${env.nameHtml(nd, t.disp)}</span>${pct}${tg}</div>`);
      addPos(t, b.x + lx, b.y + ly, b.tw, TH, "tile");
    });
    return `<div class="mblock" style="left:${b.x}px;top:${b.y}px;width:${b.w}px;height:${b.h}px">${parts.join("")}</div>`;
  };
  const placeDown = (n, x, y0) => {
    const y = y0 + n.a - BH / 2;
    out.html.push(n.kind === "root" ? rootHtml(x, y) : boxHtml(n, x, y));
    addPos(n, x, y, BW, BH, n.kind === "root" ? "root" : "box");
    if (!n.open) return;
    const cx = x + BW + CG, mx = x + BW + TRUNK, py = y0 + n.a;
    const ends = [];
    let right = cx + BW;
    // The group label sits just above the first box (a tall branch can push that box down).
    const first = n.kids[0];
    if (n.label) out.html.push(`<div class="glabel" style="left:${cx}px;top:${first ? y0 + first.y0 + first.a - BH / 2 - GL + 1 : y0 + 1}px">${n.label}</div>`);
    for (const k of n.kids) { placeDown(k, cx, y0 + k.y0); ends.push([y0 + k.y0 + k.a, cx]); }
    if (n.block) {
      const b = n.block;
      b.x = cx;
      b.y = y0 + b.y0;
      out.blocks.push(b);
      out.html.push(blockHtml(b));
      // The line ends at the block's own frame; that frame already groups its tiles.
      ends.push([b.y + b.a, cx]);
      right = Math.max(right, cx + b.w);
    }
    const ys = ends.map((e) => e[0]).concat(py);
    out.lines.push(`M${P(x + BW)} ${P(py)}H${P(mx)}M${P(mx)} ${P(Math.min(...ys))}V${P(Math.max(...ys))}`
      + ends.map(([ay, ex]) => `M${P(mx)} ${P(ay)}H${P(ex)}`).join(""));
    // Each box's stake on its stretch from the bracket (the block's tiles carry their own).
    const level = ends.length === 1 && Math.abs(ends[0][0] - py) < 1;
    n.kids.forEach((k, i) => stake(level ? x + BW : mx, cx, ends[i][0], k));
    out.kidsBox[n.id] = { x: cx, y: y0, w: right - cx, h: n.h };
  };
  const placeUp = (n, depth, y0, rootX) => {
    const x = rootX - depth * (BW + CG), y = y0 + n.top;
    out.html.push(boxHtml(n, x, y));
    addPos(n, x, y, BW, BH, "box");
    if (!n.kids.length) return;
    const ends = n.kids.map((k) => { placeUp(k, depth + 1, y0 + k.y0, rootX); return y0 + k.y0 + k.a; });
    upLines(x, y + BH / 2, ends, n.kids);
  };

  const owners = buildUp(d.orgnr, "root", 100, d.orgnr, new Set()).kids;
  owners.forEach(measureUp);
  let y = 0;
  owners.forEach((o) => { o.y0 = y; y += o.h + VG; });
  const mU = owners.length ? (owners[0].y0 + owners[0].a + owners[owners.length - 1].y0 + owners[owners.length - 1].a) / 2 : 0;
  const rootX = PAD + (owners.length ? Math.max(...owners.map(upCols)) : 0) * (BW + CG);
  const root = buildDown(d.orgnr, "root", 100, 0, d.orgnr, new Set());
  measureDown(root, rootX);
  const downY0 = PAD + Math.max(0, mU - root.a), upY0 = downY0 + root.a - mU;
  placeDown(root, rootX, downY0);
  if (owners.length) {
    const ends = owners.map((o) => { placeUp(o, 1, upY0 + o.y0, rootX); return upY0 + o.y0 + o.a; });
    upLines(rootX, downY0 + root.a, ends, owners);
  }
  let W = 0, H = 0;
  for (const r of out.rects) { W = Math.max(W, r.x + r.w); H = Math.max(H, r.y + r.h); }
  out.W = W + PAD;
  out.H = H + PAD;
  out.cand = cand;
  out.capped = capped;
  out.hits = [...cand].filter((k) => k in out.pos).sort((a, b) => out.pos[a].x - out.pos[b].x || out.pos[a].y - out.pos[b].y);
  return out;
}


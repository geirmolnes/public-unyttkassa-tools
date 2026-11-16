// Eierkart layout (src/static/eierkart.js) on made-up groups shaped like the real cases:
// one company holding dozens of look-alike companies (Læringsverkstedet), a deeper group mixing
// branches, small stakes and a company listed twice (HJR Holding), and owners that own the
// company through several others (NorgesGruppen). Run with `bun test`.
import { describe, expect, test } from "bun:test";
import { esc } from "../../src/static/format.js";
import { FIT_MIN, MAP, clampView, fitView, layoutMap, mergeFollow, nameParts, pathIds, stakeLabel, stripName, treeIndex } from "../../src/static/eierkart.js";

const COL = MAP.BW + MAP.CG; // one column of boxes and its gap

const PLACES = ["ALVDAL", "ASKIM", "BAMBLE", "BERGEN", "BODØ", "DRAMMEN", "EIDSVOLL", "FARSUND", "FLORØ", "GJØVIK",
  "HALDEN", "HAMAR", "HORTEN", "KONGSBERG", "KRAGERØ", "LARVIK", "LILLEHAMMER", "MANDAL", "MOLDE", "MOSS",
  "NARVIK", "NOTODDEN", "ODDA", "RISØR", "RØROS", "SANDEFJORD", "SKIEN", "STAVANGER", "TROMSØ", "TØNSBERG",
  "ULSTEINVIK", "VADSØ", "VARDØ", "ØRSTA", "ØYER", "ÅLESUND", "ÅRDAL", "ÅS"];
const orgnr = (n) => String(910000000 + n);
const ENV = {
  width: 1400,
  height: 800,
  measure: (text, condensed) => text.length * (condensed ? 6 : 7),
  nameHtml: (node, text = node.name) => esc(text),
};

/** A structure as the API returns it. `holdings` maps parent → [[key, name, pct, kind]]. */
function structure(holdings, owners) {
  const nodes = { [orgnr(0)]: { name: "ACME AS", orgnr: orgnr(0) } };
  const down = {}, up = {}, under = {};
  for (const [parent, list] of Object.entries(holdings)) {
    down[parent] = list.map(([key, name, pct, kind]) => {
      nodes[key] = nodes[key] || { name, orgnr: key };
      return [key, pct, kind];
    });
  }
  // Companies below along «own» edges, each once, as the API counts them.
  const below = (key, seen = new Set()) => {
    for (const [k, , kind] of down[key] || []) if (kind === "own" && !seen.has(k)) { seen.add(k); below(k, seen); }
    return seen;
  };
  Object.keys(nodes).forEach((k) => { const b = below(k); b.delete(k); under[k] = b.size; });
  for (const [child, list] of Object.entries(owners)) {
    up[child] = list.map(([key, node, pct, kind = "own"]) => { nodes[key] = node; return [key, pct, kind]; });
  }
  return {
    orgnr: orgnr(0), name: "ACME AS", nodes, down, up, under,
    rules: { follow: "minst 20 %", below: "under 20 %", owner_pct: 5 },
    stats: { companies: under[orgnr(0)], down_depth: 1, owners: Object.keys(up).length, up_depth: 1 },
  };
}

// Three companies that each have one owner, then two people at the top.
const CHAIN = {
  [orgnr(0)]: [[orgnr(901), { name: "ACME GRUPPEN AS", orgnr: orgnr(901) }, 100]],
  [orgnr(901)]: [[orgnr(902), { name: "ACME INVEST AS", orgnr: orgnr(902) }, 100]],
  [orgnr(902)]: [[orgnr(903), { name: "ACME HOLDING AS", orgnr: orgnr(903) }, 100]],
  [orgnr(903)]: [
    ["KARI NORDMANN|1970", { name: "KARI NORDMANN", orgnr: null, birth_year: 1970 }, 50],
    ["OLA NORDMANN|1968", { name: "OLA NORDMANN", orgnr: null, birth_year: 1968 }, 50],
  ],
};

/** The map as the view first shows it: the company open one level down, owners all the way up. */
function view(d, extra = {}) {
  return { data: d, parent: treeIndex(d.down, d.orgnr).parent, upParent: treeIndex(d.up, d.orgnr).parent,
           open: {}, upOpen: {}, level: 1, small: true, q: "", extra: {}, ...extra };
}
const ROOT = orgnr(0);
const at = (...keys) => [ROOT, ...keys].join(">"); // a place on the holdings side
const up = (...keys) => [ROOT, ...keys].join("<"); // and on the owners side

const wide = () => structure({ [orgnr(0)]: PLACES.map((p, i) => [orgnr(i + 1), `ACME ${p} BARNEHAGE AS`, 100, "own"]) }, CHAIN);

// Twelve companies at different stakes; the first two hold companies of their own, one of which
// the top company holds too (drawn in both places); one small stake.
function deep() {
  const kids = PLACES.slice(0, 12).map((p, i) => [orgnr(i + 1), `BETA ${p} AS`, i === 5 ? 60 : 100, "own"]);
  return structure({
    [orgnr(0)]: [...kids, [orgnr(50), "BETA EIENDOM AS", 70, "own"], [orgnr(60), "ANNET SELSKAP AS", 4, "small"]],
    [orgnr(1)]: [[orgnr(100), "GAMMA DRIFT AS", 100, "own"], [orgnr(50), "BETA EIENDOM AS", 30, "own"]],
    [orgnr(2)]: [[orgnr(200), "DELTA AS", 51, "own"]],
    [orgnr(100)]: [[orgnr(300), "EPSILON SKJULT AS", 100, "own"]],
  }, { [orgnr(0)]: [["OLA NORDMANN|1968", { name: "OLA NORDMANN", orgnr: null, birth_year: 1968 }, 100]] });
}

const person = (name, born) => [`${name}|${born}`, { name, orgnr: null, birth_year: born }];
const company = (n, name) => [orgnr(n), { name, orgnr: orgnr(n) }];
const [BL, GR, INN, CH, INV, KV] = [801, 802, 803, 804, 805, 806].map(orgnr);
const [LARS, OLE] = ["LARS HOLM|1988", "OLE VOLD|1975"];
const [A, B, J, J1] = [501, 502, 503, 504].map(orgnr);

// NorgesGruppen's owners (the people's names are made up): CHAPEL HILL and KVARTAL own BRØDRENE LORENTZEN directly and through
// GRANSTUEN / INNOCENTI, each with one owner of its own. Below, ALFA and BETA share a JV.
function lorentzen() {
  return structure({
    [ROOT]: [[A, "ALFA AS", 60, "own"], [B, "BETA AS", 100, "own"]],
    [A]: [[J, "JV AS", 50, "own"]],
    [B]: [[J, "JV AS", 50, "own"]],
    [J]: [[J1, "JV DRIFT AS", 100, "own"]],
  }, {
    [ROOT]: [[...company(801, "BRØDRENE LORENTZEN AS"), 9]],
    [BL]: [[...company(802, "GRANSTUEN AS"), 18.08], [...company(806, "KVARTAL AS"), 17.49], [...company(803, "INNOCENTI AS"), 15.02],
           [...company(804, "CHAPEL HILL INVEST AS"), 10.64]],
    [GR]: [[...company(804, "CHAPEL HILL INVEST AS"), 33], [...company(805, "INVESTORIUS AS"), 33]],
    [INN]: [[...company(806, "KVARTAL AS"), 40], [...person("PEDER ÅS", 1944), 20]],
    [CH]: [[...person("LARS HOLM", 1988), 100]],
    [INV]: [[...person("HANS TASTAD", 1972), 100]],
    [KV]: [[...person("OLE VOLD", 1975), 100]],
  });
}

// C1 and C2 hold each other; S holds part of the company that owns it; E1 and E2 own each other.
function loops() {
  return structure({
    [ROOT]: [[orgnr(1), "C1 AS", 100, "own"], [orgnr(3), "S AS", 100, "own"]],
    [orgnr(1)]: [[orgnr(2), "C2 AS", 100, "own"]],
    [orgnr(2)]: [[orgnr(1), "C1 AS", 60, "own"]],
    [orgnr(3)]: [[ROOT, "ACME AS", 25, "own"]],
  }, {
    [ROOT]: [[...company(11, "E1 AS"), 100]],
    [orgnr(11)]: [[...company(12, "E2 AS"), 100]],
    [orgnr(12)]: [[...company(11, "E1 AS"), 60], [...person("Q", 1970), 40]],
  });
}

/** A line of `n` owners up from the company, each owning the one below: companies, a person at the top. */
function line(n) {
  const pcts = [68.18, 95.71, 100], up = {};
  let below = ROOT;
  for (let i = 1; i < n; i++) { up[below] = [[...company(700 + i, `LEDD ${i} AS`), pcts[i - 1]]]; below = orgnr(700 + i); }
  up[below] = [[...person("PER EIER", 1958), pcts[n - 1]]];
  return structure({}, up);
}

/** `k` companies own the company, each owned by one person. */
function owners(k) {
  const up = { [ROOT]: [] };
  for (let i = 1; i <= k; i++) {
    up[ROOT].push([...company(1000 + i, `EIER ${i} AS`), 0.1]);
    up[orgnr(1000 + i)] = [[...person(`P${i}`, 1970), 100]];
  }
  return structure({}, up);
}

const html = (L) => L.html.join("");
/** Stake labels on the lines, in drawing order: {x, y, text, soft}. */
const stakes = (L) => [...html(L).matchAll(/<span class="ep num( soft)?" style="left:([\d.]+)px;top:([\d.]+)px">([^<]+)<\/span>/g)]
  .map(([, soft, x, y, text]) => ({ x: Number(x), y: Number(y), text, soft: !!soft }));
const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

describe("names", () => {
  test("the shared words are cut, and each keeps at least one", () => {
    const g = nameParts(["LÆRINGSVERKSTEDET ALVERSUND BARNEHAGE AS", "LÆRINGSVERKSTEDET ÅSE BARNEHAGE AS"], true);
    expect(g).toEqual({ pre: 1, suf: 2, prefix: "LÆRINGSVERKSTEDET", suffix: "BARNEHAGE AS" });
    expect(stripName("LÆRINGSVERKSTEDET ÅSE BARNEHAGE AS", g.pre, g.suf)).toBe("ÅSE");
    expect(nameParts(["ACME AS", "ACME AS"], true)).toMatchObject({ pre: 1, suf: 0 });
    expect(nameParts(["ALFA AS"], true)).toMatchObject({ pre: 0, suf: 0 });
  });

  test("a suffix is only cut inside a block", () => {
    expect(nameParts(["A X AS", "A Y AS"], false)).toMatchObject({ pre: 1, suf: 0, suffix: "" });
  });
});

describe("tree index", () => {
  test("a company listed twice gets the parent nearest the top", () => {
    const { parent, depth } = treeIndex(deep().down, ROOT);
    expect([parent[orgnr(50)], depth[orgnr(50)], depth[orgnr(300)]]).toEqual([ROOT, 1, 3]);
  });

  test.each([
    ["three levels down", orgnr(300), [ROOT, at(orgnr(1)), at(orgnr(1), orgnr(100))]],
    ["one level down", orgnr(1), [ROOT]],
    ["the company itself", ROOT, []],
    ["not in the map", orgnr(999), []],
  ])("the places on the way to a company %s", (_, key, ids) => {
    expect(pathIds(treeIndex(deep().down, ROOT).parent, key, ">")).toEqual(ids);
  });
});

describe("a long list of look-alike companies", () => {
  const L = layoutMap(view(wide()), ENV);
  const tiles = L.rects.filter((r) => r.type === "tile");
  const block = L.blocks[0];

  test("becomes one block of tiles", () => {
    expect(L.blocks).toHaveLength(1);
    expect(tiles).toHaveLength(PLACES.length);
    expect(L.rects.filter((r) => r.type === "box")).toHaveLength(5); // three owners in a line and the two people
  });

  test("says the shared name and stake once, and not on each tile", () => {
    expect(block.pattern).toBe("ACME … BARNEHAGE AS");
    expect(block.meta).toContain("alle 100 %");
    expect(block.meta).toContain(`${PLACES.length} selskaper`);
    expect(html(L)).not.toContain('class="tp"');
    expect(html(L)).toContain('<span class="tn">ALVDAL</span>');
  });

  // Regression: a «[» bracket beside the block doubled its frame as a stray line along its whole height.
  test("is joined by its line straight to its frame, with no bracket beside it", () => {
    expect(L.brackets).toBeUndefined();
    const lines = L.lines.join("");
    expect(lines).toContain(`H${Math.round(block.x) + 0.5}`);
    expect(lines).not.toContain(`H${Math.round(block.x - 10) + 0.5}`);
  });

  test("runs A–Å in Norwegian order, down each column", () => {
    const names = block.tiles.map((t) => t.disp);
    expect(names[0]).toBe("ALVDAL");
    expect(names.slice(-3)).toEqual(["ÅLESUND", "ÅRDAL", "ÅS"]);
    expect(names.indexOf("ØRSTA")).toBeGreaterThan(names.indexOf("VARDØ"));
    const first = L.pos[block.tiles[0].key], second = L.pos[block.tiles[1].key];
    expect(second.x).toBe(first.x);
    expect(second.y).toBe(first.y + MAP.TH);
  });

  test("takes columns so the whole map fits the frame at full size", () => {
    const env = { ...ENV, width: 2000 }; // room for the four owner columns as well
    const W = layoutMap(view(wide()), env);
    expect(PLACES.length * MAP.TH).toBeGreaterThan(env.height); // one column wouldn't
    expect(W.blocks[0].cols).toBeGreaterThan(1);
    expect(fitView(W.W, W.H, env.width, env.height, W.pos[orgnr(0)]).k).toBe(1);
  });

  test("a closed owner with one owner says «1 eier»", () => {
    const v = view(wide());
    v.upOpen[up(orgnr(901))] = false;
    const h = html(layoutMap(v, ENV));
    expect(h).toContain("▸ 1 eier</button>");
  });
});

describe("a deeper group", () => {
  test("companies with holdings stay boxes above the block, so they can open", () => {
    const L = layoutMap(view(deep()), ENV);
    expect(L.blocks).toHaveLength(1);
    expect(L.pos[orgnr(1)]).toMatchObject({ w: MAP.BW, h: MAP.BH });
    expect(L.blocks[0].tiles.map((t) => t.key)).not.toContain(orgnr(1));
    expect(L.blocks[0].tiles).toHaveLength(12);
    expect(L.pos[orgnr(1)].y).toBeLessThan(L.blocks[0].y);
    expect(html(L)).toContain("▸ 3 under");
  });

  test("tiles show their stake when the stakes differ", () => {
    const L = layoutMap(view(deep()), ENV);
    expect(L.blocks[0].uniform).toBeNull();
    expect(html(L)).toContain('<span class="tp">60 %</span>');
    expect(html(L)).toContain('<span class="tp">4 %</span>');
  });

  test("small stakes go when they're hidden", () => {
    expect(layoutMap(view(deep()), ENV).pos[orgnr(60)]).toBeTruthy();
    expect(layoutMap(view(deep(), { small: false }), ENV).pos[orgnr(60)]).toBeUndefined();
  });

  test("a company listed twice is drawn in both places, neither dashed", () => {
    const v = view(deep());
    v.open[at(orgnr(1))] = true;
    const L = layoutMap(v, ENV);
    expect(L.spots[orgnr(50)]).toHaveLength(2);
    expect(html(L)).not.toMatch(new RegExp(`dashed" data-k="${orgnr(50)}"`));
    expect(L.pos[orgnr(50)]).toBe(L.at[at(orgnr(1), orgnr(50))]); // pos is the first drawn
  });

  test("search opens the path to a hit and finds people", () => {
    const L = layoutMap(view(deep(), { q: "epsilon" }), ENV);
    expect(L.hits).toEqual([orgnr(300)]);
    expect(L.pos[orgnr(300)]).toBeTruthy();
    expect(layoutMap(view(deep(), { q: "nordmann" }), ENV).hits).toEqual(["OLA NORDMANN|1968"]);
    expect(layoutMap(view(deep(), { q: "e" }), ENV).hits).toEqual([]); // two letters at least
  });

  test("nothing drawn overlaps", () => {
    for (const L of [layoutMap(view(deep(), { level: Infinity }), ENV), layoutMap(view(wide()), ENV), layoutMap(view(lorentzen()), ENV)]) {
      const top = L.rects.filter((r) => r.type !== "tile");
      const tiles = L.rects.filter((r) => r.type === "tile");
      for (const group of [top, tiles]) {
        group.forEach((a, i) => group.slice(i + 1).forEach((b) => expect(overlaps(a, b)).toBe(false)));
      }
      L.rects.forEach((r) => {
        expect(r.x).toBeGreaterThanOrEqual(0);
        expect(r.y).toBeGreaterThanOrEqual(0);
        expect(r.x + r.w).toBeLessThanOrEqual(L.W);
        expect(r.y + r.h).toBeLessThanOrEqual(L.H);
      });
    }
  });
});

// Regression: a run of owners that each had one owner was folded into one «Eierkjede» box.
describe("a line of owners", () => {
  test.each([
    [1, [[COL, 0]]],
    [2, [[COL, 0], [2 * COL, 0]]],
    [3, [[COL, 0], [2 * COL, 0], [3 * COL, 0]]],
  ])("%i owners: one box each, a column apart, on the company's line", (n, offsets) => {
    const L = layoutMap(view(line(n)), ENV);
    const root = L.pos[ROOT];
    expect(L.rects.filter((r) => r.type === "box").map((r) => [root.x - r.x, r.y - root.y])).toEqual(offsets);
  });

  test("each owner is an ordinary box, joined by a line that carries its stake", () => {
    const L = layoutMap(view(line(3)), ENV);
    expect([html(L).includes("EIERKJEDE"), html(L).includes('class="stack"'), L.lines.length]).toEqual([false, false, 3]);
    expect(html(L)).toMatch(/<span class="l1"><span class="chip">[^<]*<button[^>]*>▾ 1 eier<\/button><\/span><\/span>\s*<span class="l2">LEDD 1 AS/);
    expect(html(L)).toMatch(/class="mb person" data-k="PER EIER\|1958"[^>]*><span class="l1"><span class="chip">f\. 1958<\/span>/);
    expect(stakes(L).map((s) => s.text)).toEqual(["100 %", "95,71 %", "68,18 %"]);
  });
});

// Regression: the stake stood in the owned box's top line, as if it were the box's own; it belongs
// to the edge between owner and owned (the user: «typed between the owner and subsidiary»).
describe("stakes on the lines", () => {
  const BH2 = MAP.BH / 2;
  const P = (v) => Math.round(v) + 0.5;
  /** Labels between the boxes `left` and `right` (place rects), on the line at height y. */
  const onEdge = (L, left, right, y) => stakes(L).filter((s) => s.x > left.x + left.w && s.x < right.x && s.y === P(y));

  test("a bracket of owners: one label each, its own stake, on its own stub short of the bracket", () => {
    const L = layoutMap(view(lorentzen()), ENV);
    const owned = L.at[up(BL)];
    for (const [key, pct] of [[GR, "18,08 %"], [KV, "17,49 %"], [INN, "15,02 %"], [CH, "10,64 %"]]) {
      const owner = L.at[up(BL, key)];
      const found = onEdge(L, owner, owned, owner.y + BH2);
      expect(found.map((s) => [s.text, s.x, s.soft])).toEqual([[pct, owned.x - (MAP.CG + MAP.TRUNK) / 2, false]]);
    }
  });

  test("one owner level with what it owns: the label sits mid-gap", () => {
    const L = layoutMap(view(line(1)), ENV);
    const [owner, root] = [L.pos["PER EIER|1958"], L.pos[ROOT]];
    expect(onEdge(L, owner, root, root.y + BH2).map((s) => [s.text, s.x])).toEqual([["68,18 %", root.x - MAP.CG / 2]]);
  });

  test("holdings: one label per company, on its own drop from the bracket; under 20 % muted", () => {
    const d = structure({ [ROOT]: [[orgnr(1), "X AS", 100, "own"], [orgnr(2), "Y AS", 51, "own"], [orgnr(3), "Z AS", 4, "small"]] }, {});
    const L = layoutMap(view(d), ENV);
    const root = L.pos[ROOT];
    const got = [1, 2, 3].map((i) => { const b = L.pos[orgnr(i)]; return onEdge(L, root, b, b.y + BH2).map((s) => [s.text, s.soft, s.x]); });
    const x = root.x + MAP.BW + MAP.TRUNK + (MAP.CG - MAP.TRUNK) / 2;
    expect(got).toEqual([[["100 %", false, x]], [["51 %", false, x]], [["4 %", true, x]]]);
  });

  test("the same stake on every company: said on each line, no «alle 100 %» above them", () => {
    const d = structure({ [ROOT]: [[orgnr(1), "X AS", 100, "own"], [orgnr(2), "Y AS", 100, "own"]] }, {});
    const L = layoutMap(view(d), ENV);
    expect([stakes(L).map((s) => s.text), html(L).includes("alle 100 %"), html(L).includes('class="glabel"')]).toEqual([["100 %", "100 %"], false, false]);
  });

  test("ownership that goes in a ring: the stake on the stop is muted", () => {
    const L = layoutMap(view(loops(), { level: Infinity }), ENV);
    const stop = L.at[up(orgnr(11), orgnr(12), orgnr(11))], below = L.at[up(orgnr(11), orgnr(12))];
    expect(onEdge(L, stop, below, stop.y + BH2).map((s) => [s.text, s.soft])).toEqual([["60 %", true]]);
  });

  test("one label per edge to a box; a block's tiles keep theirs and its line has none", () => {
    const L = layoutMap(view(wide()), ENV);
    expect([stakes(L).length, L.rects.filter((r) => r.type === "box").length]).toEqual([5, 5]);
    expect(L.blocks[0].meta).toContain("alle 100 %");
  });

  test("no box shows a stake; one with no chip shows just its name", () => {
    const d = structure({ [ROOT]: [[orgnr(1), "X AS", 51, "own"]] }, CHAIN);
    const h = html(layoutMap(view(d), ENV));
    const boxes = h.match(/<div class="mb(?! root)[^"]*"[^]*?<\/div>/g);
    expect(boxes).toHaveLength(6);
    for (const b of boxes) expect([b.includes('class="num"'), /\d %/.test(b)]).toEqual([false, false]);
    expect(h).toMatch(new RegExp(`data-k="${orgnr(1)}"[^>]*>\\s*<span class="l2">X AS</span>`));
  });

  test("the label: the stake, muted on request", () => {
    expect([stakeLabel(68.1813), stakeLabel(8.75, true)]).toEqual(['<span class="ep num">68,18 %</span>', '<span class="ep num soft">8,75 %</span>']);
  });
});

describe("a company in several places", () => {
  // Regression: these were dashed «se over» boxes, with the owner's own owners drawn only once.
  test.each([
    ["CHAPEL HILL, under BRØDRENE LORENTZEN and under GRANSTUEN", CH, LARS],
    ["KVARTAL, under BRØDRENE LORENTZEN and under INNOCENTI", KV, OLE],
  ])("an owner of %s, has its own owner above it in both", (_, key, owner) => {
    const L = layoutMap(view(lorentzen()), ENV);
    expect(L.spots[key]).toHaveLength(2);
    expect(L.spots[owner]).toEqual(L.spots[key].map((r) => ({ ...r, x: r.x - COL }))); // one column out, on the same line
  });

  test("no place is dashed or sent elsewhere", () => {
    const h = html(layoutMap(view(lorentzen(), { level: Infinity }), ENV));
    expect([h.includes("se over"), h.includes("vist et annet sted"), h.includes("dashed")]).toEqual([false, false, false]);
  });

  test("a company two parents hold is drawn whole under each", () => {
    const L = layoutMap(view(lorentzen(), { level: Infinity }), ENV);
    expect([L.spots[J1].length, html(L).match(/▾ 2 under/g).length]).toEqual([2, 2]);
  });

  test("each place opens on its own", () => {
    const v = view(lorentzen(), { level: 2 });
    v.open[at(A, J)] = true;
    const L = layoutMap(v, ENV);
    expect([!!L.at[at(A, J, J1)], !!L.at[at(B, J, J1)], !!L.kidsBox[at(A, J)]]).toEqual([true, false, true]);
  });

  test("a search hit is marked in every place, and counted once", () => {
    const L = layoutMap(view(lorentzen(), { q: "lars" }), ENV);
    expect([L.hits, html(L).match(/class="mb person hit"/g).length]).toEqual([[LARS], 2]);
  });
});

describe("a loop", () => {
  test.each([
    ["two companies holding each other", orgnr(1)],
    ["a company holding part of its owner", ROOT],
    ["two owners owning each other", orgnr(11)],
  ])("%s: drawn once round, then a stop marked «går i ring»", (_, key) => {
    const L = layoutMap(view(loops(), { level: Infinity }), ENV);
    const stops = L.html.filter((h) => h.includes(`data-k="${key}"`) && h.includes("går i ring"));
    expect([L.spots[key].length, stops.length, stops[0].includes("data-mkey"), stops[0].includes("dashed")]).toEqual([2, 1, false, true]);
  });

  test("only loops stop", () => {
    expect(html(layoutMap(view(loops(), { level: Infinity }), ENV)).match(/class="chip">går i ring/g)).toHaveLength(3);
    expect(html(layoutMap(view(lorentzen(), { level: Infinity }), ENV))).not.toContain("går i ring");
  });
});

describe("a very big map", () => {
  // 2000 boxes a side open by themselves: the k companies, then one owner each while there's room.
  test.each([
    [999, false, 999],
    [1000, false, 1000],
    [1001, true, 999],
  ])("%i companies with an owner each: capped %p, %i owners drawn", (k, capped, drawn) => {
    const L = layoutMap(view(owners(k)), ENV);
    expect([L.capped, Object.keys(L.spots).filter((key) => key.startsWith("P")).length]).toEqual([capped, drawn]);
  });

  test("a place clicked open past the cap still opens", () => {
    const v = view(owners(1001));
    v.upOpen[up(orgnr(2001))] = true;
    expect(layoutMap(v, ENV).spots["P1001|1970"]).toHaveLength(1);
  });
});

describe("zoom", () => {
  const root = { x: 500, y: 300, w: MAP.BW, h: MAP.BH };

  test("a small map shows whole, never enlarged", () => {
    const v = fitView(600, 300, 1400, 800, root, true);
    expect(v.k).toBe(1);
    expect(v.tx).toBe(400);
  });

  test("a map too big to read opens on the company", () => {
    const v = fitView(8000, 6000, 1400, 800, root, true);
    expect(v.k).toBeGreaterThan(FIT_MIN);
    expect(v.tx + (root.x + root.w / 2) * v.k).toBeCloseTo(700);
    expect(v.ty + (root.y + root.h / 2) * v.k).toBeCloseTo(400);
    expect(fitView(8000, 6000, 1400, 800, root, false).k).toBeLessThan(FIT_MIN); // «Tilpass» still fits it all
  });

  test("panning keeps part of the map in the frame", () => {
    expect(clampView({ k: 1, tx: 5000, ty: -5000 }, 2000, 1000, 1400, 800)).toEqual({ tx: 1320, ty: -920 });
  });
});

// deep()'s small stake ANNET SELSKAP AS (orgnr 60) holds two companies: one new, and BETA
// EIENDOM AS (orgnr 50), which the map already opens under the top company.
const followSub = () => ({
  edges: { [orgnr(60)]: [[orgnr(70), 100, "own"], [orgnr(50), 40, "own"]], [orgnr(70)]: [[orgnr(71), 100, "own"]] },
  names: { [orgnr(70)]: "ZETA AS", [orgnr(71)]: "ZETA DRIFT AS", [orgnr(50)]: "BETA EIENDOM AS" },
  under: { [orgnr(60)]: 3, [orgnr(70)]: 1, [orgnr(71)]: 0, [orgnr(50)]: 0 },
  more: { [orgnr(72)]: 4 },
});

/** deep() with ANNET SELSKAP AS holding two companies, in the given mode, optionally followed. */
function followView(follow, followed = false) {
  const d = deep();
  d.more = { [orgnr(60)]: 2 };
  if (followed) mergeFollow(d, followSub());
  const v = view(d, { follow, parent: treeIndex(d.down, d.orgnr, follow).parent });
  if (followed) v.open[at(orgnr(60))] = true;
  return { d, v, L: layoutMap(v, ENV) };
}

describe("following a stake under the line", () => {
  test("what the map already has stays; the rest is added with its counts", () => {
    const { d } = followView(true, true);
    expect(d.down[orgnr(60)]).toEqual([[orgnr(70), 100, "own"], [orgnr(50), 40, "own"]]);
    expect([d.under[orgnr(60)], d.under[orgnr(70)], d.under[orgnr(50)], d.nodes[orgnr(71)].name, d.more[orgnr(72)]])
      .toEqual([3, 1, 0, "ZETA DRIFT AS", 4]);
  });

  test("the followed company counts as opened, under its first parent", () => {
    const d = deep();
    mergeFollow(d, followSub());
    const { parent, depth } = treeIndex(d.down, d.orgnr, true);
    expect([parent[orgnr(71)], depth[orgnr(71)], depth[orgnr(50)]]).toEqual([orgnr(70), 3, 1]);
    expect(orgnr(60) in treeIndex(d.down, d.orgnr).depth).toBe(false);
  });

  test.each([
    ["follow on", true, true],
    ["follow off", false, false],
  ])("%s: the stake gets a button to follow it", (_, follow, shown) => {
    expect(html(followView(follow).L).includes(`data-follow="${orgnr(60)}"`)).toBe(shown);
  });

  test("a stake holding nothing gets no button", () => {
    const d = deep();
    const L = layoutMap(view(d, { follow: true }), ENV);
    expect(html(L)).not.toContain("data-follow");
    expect(html(L)).toContain("under 20 %, ikke fulgt");
  });

  test("once followed it opens like any company, still dashed", () => {
    const { L } = followView(true, true);
    expect(html(L)).not.toContain(`data-follow="${orgnr(60)}"`);
    expect(html(L)).toMatch(new RegExp(`class="mb dashed" data-k="${orgnr(60)}"[^]*?▾ 3 under`));
    expect(L.pos[orgnr(70)]).toBeDefined();
  });

  test("switching follow off closes it again", () => {
    const d = deep();
    d.more = { [orgnr(60)]: 2 };
    mergeFollow(d, followSub());
    const v = view(d, { follow: false });
    v.open[at(orgnr(60))] = true;
    expect(layoutMap(v, ENV).pos[orgnr(70)]).toBeUndefined();
  });
});

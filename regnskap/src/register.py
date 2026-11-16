"""Read side of the shareholder register for the company view: owners, chain upwards, holdings,
year-over-year changes, final owners and the eierkart (the group both ways)."""

import itertools
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from src import shareholders

# Companies above this many holding rows (all years) are the big listed ones (Equinor: 708k);
# they get a top list only, no change tags or history.
HISTORY_MAX_ROWS = 5000
TOP_HOLDERS = 100
MAX_OWNS = 1000
CHAIN_DEPTH = 6
CHAIN_MIN_PCT = 5.0
CHAIN_PER_LEVEL = 10
HISTORY_ROWS = 100
HISTORY_CHANGES = 300
# Final owners: stakes multiplied through every level. Stake below ULTIMATE_EPS (% of the company)
# isn't passed on; owners under ULTIMATE_MIN_PCT in total count as spread.
ULTIMATE_EPS = 0.01
ULTIMATE_MIN_PCT = 1.0
ULTIMATE_MAX = 20
ULTIMATE_MAX_STEPS = 5000
# Structure (eierkart): owners upwards at CHAIN_MIN_PCT per level, holdings downwards per `follows`.
STRUCTURE_DEPTH = 15
STRUCTURE_MAX_COMPANIES = 3000
MAP_BRANCHES = 4
# Saved structures carry this; bump it when the rules change so old ones are rebuilt.
STRUCTURE_VERSION = 3
# How the UI words `follows`: the stakes that are followed, and the ones that aren't.
FOLLOW_RULE = "minst 20 %"
FOLLOW_BELOW = "under 20 %"

HOLDING_COLS = """h.year, h.class_id, sc.name AS class, h.holder_name AS name,
    h.holder_orgnr AS orgnr, h.holder_birth_year AS birth_year, h.holder_postcode AS postcode,
    h.holder_place AS place, h.country, h.shares, cc.total_shares AS total"""
HOLDING_FROM = """FROM holdings h
    JOIN company_classes cc USING (year, orgnr, class_id)
    JOIN share_classes sc ON sc.id = h.class_id"""
# One row per holder across share classes (grouped like holder_key). The register repeats the
# company's total on every class row, so summed shares over that total is the holder's stake.
CHAIN_HOLDERS = f"""SELECT h.holder_name AS name, h.holder_orgnr AS orgnr,
    h.holder_birth_year AS birth_year, h.holder_postcode AS postcode, h.holder_place AS place,
    h.country, GROUP_CONCAT(DISTINCT sc.name) AS class, SUM(h.shares) AS shares,
    MAX(cc.total_shares) AS total {HOLDING_FROM}
    WHERE h.orgnr = ? AND h.year = ? AND (h.holder_orgnr IS NULL OR h.holder_orgnr != h.orgnr)
    GROUP BY COALESCE(h.holder_orgnr,
        COALESCE(h.holder_name, '') || '|' || COALESCE(h.holder_birth_year, ''))
    HAVING SUM(h.shares) * 100.0 >= MAX(cc.total_shares) * ?"""
# What one company holds, one row per company across share classes (same stake rule as above).
HOLDS = """SELECT h.orgnr, c.name, SUM(h.shares) AS shares, MAX(cc.total_shares) AS total
    FROM holdings h JOIN companies c USING (year, orgnr)
    JOIN company_classes cc USING (year, orgnr, class_id)
    WHERE h.holder_orgnr = ? AND h.year = ? AND h.orgnr != h.holder_orgnr
    GROUP BY h.orgnr"""
# The same for a person (name + birth year; the register has no national ID).
PERSON_HOLDS = """SELECT h.orgnr, c.name, SUM(h.shares) AS shares, MAX(cc.total_shares) AS total
    FROM holdings h JOIN companies c USING (year, orgnr)
    JOIN company_classes cc USING (year, orgnr, class_id)
    WHERE h.holder_name = ? AND h.holder_birth_year IS ? AND h.holder_orgnr IS NULL AND h.year = ?
    GROUP BY h.orgnr"""


def follows(stake: float, through: float) -> bool:
    """Whether the eierkart goes on down into a company held with `stake` % by its parent, where
    the company at the top holds `through` % of it once every level is multiplied in.

    Companies held below the line are still listed (as «ikke fulgt»), just not opened. Bump
    STRUCTURE_VERSION and reword FOLLOW_RULE and FOLLOW_BELOW with any change here."""
    # TODO(human): the rule for what counts as part of the group (see FOLLOW_RULE).
    return stake >= 20.0


class Unavailable(Exception):
    """The register database isn't present (not built locally / not mounted)."""


def _connect() -> sqlite3.Connection:
    try:
        return shareholders.open_live()
    except FileNotFoundError as e:
        raise Unavailable(str(e)) from e


def pct(shares: int, total: int | None) -> float | None:
    return round(shares * 100 / total, 4) if total else None


def holder_key(row: dict[str, Any]) -> str:
    """Same holder across years: orgnr for companies, name + birth year for people.
    The address is left out so a move doesn't read as a sale."""
    return row["orgnr"] or f"{row['name']}|{row['birth_year'] or ''}"


@dataclass
class Holding:
    key: str
    class_id: int
    name: str | None
    orgnr: str | None
    birth_year: int | None
    postcode: str | None
    place: str | None
    country: str | None
    class_name: str
    shares: int = 0
    total: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "orgnr": self.orgnr,
            "birth_year": self.birth_year,
            "postcode": self.postcode,
            "place": self.place,
            "country": self.country,
            "class": self.class_name,
            "shares": self.shares,
            "total": self.total,
            "pct": pct(self.shares, self.total),
        }


@dataclass
class History:
    """All years of one company's holders, keyed (holder, class) → year → Holding."""

    years: list[int]
    by_key: dict[tuple[str, int], dict[int, Holding]] = field(default_factory=dict)

    def year(self, y: int) -> list[Holding]:
        return sorted(
            (by[y] for by in self.by_key.values() if y in by),
            key=lambda h: (-h.shares, h.name or ""),
        )


def _same_share(a: Holding, b: Holding) -> bool:
    return a.total is not None and b.total is not None and a.shares * b.total == b.shares * a.total


def _load_history(conn: sqlite3.Connection, orgnr: str, years: list[int]) -> History:
    hist = History(years)
    rows = conn.execute(f"SELECT {HOLDING_COLS} {HOLDING_FROM} WHERE h.orgnr = ?", (orgnr,))
    for r in map(dict, rows):
        key = (holder_key(r), r["class_id"])
        by = hist.by_key.setdefault(key, {})
        h = by.get(r["year"])
        if h is None:
            h = by[r["year"]] = Holding(
                key[0], r["class_id"], r["name"], r["orgnr"], r["birth_year"], r["postcode"],
                r["place"], r["country"], r["class"], total=r["total"],
            )  # fmt: skip
        h.shares += r["shares"]  # same holder, other address: one holding
    return hist


def changes(hist: History) -> list[dict[str, Any]]:
    """In / out / changed share per holder and class, oldest first."""
    out = []
    for prev, y in itertools.pairwise(hist.years):
        for by in hist.by_key.values():
            a, b = by.get(prev), by.get(y)
            if a is None and b is None:
                continue
            if a is None:
                kind = "in"
            elif b is None:
                kind = "out"
            elif not _same_share(a, b):
                kind = "changed"
            else:
                continue
            h = b or a
            out.append(
                {
                    "year": y,
                    "kind": kind,
                    **{
                        k: v
                        for k, v in h.to_dict().items()
                        if k in ("name", "orgnr", "birth_year", "class")
                    },
                    "from_pct": pct(a.shares, a.total) if a else None,
                    "to_pct": pct(b.shares, b.total) if b else None,
                    "from_shares": a.shares if a else None,
                    "to_shares": b.shares if b else None,
                }
            )
    out.sort(key=lambda c: (c["year"], c["kind"] != "out", -(c["to_pct"] or c["from_pct"] or 0)))
    return out


def _tag(hist: History, h: Holding, y: int) -> str | None:
    """new / up / down versus the previous register year; None in the first year."""
    i = hist.years.index(y)
    if i == 0:
        return None
    prev = hist.by_key[(h.key, h.class_id)].get(hist.years[i - 1])
    if prev is None:
        return "new"
    if _same_share(prev, h):
        return None
    return "up" if h.shares * (prev.total or 0) > prev.shares * (h.total or 0) else "down"


def _years(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT year FROM imports ORDER BY year")]


def _chain(conn: sqlite3.Connection, orgnr: str, year: int) -> list[list[dict[str, Any]]]:
    """Owners level by level upwards: holders with ≥ CHAIN_MIN_PCT, recursing into companies."""
    levels: list[list[dict[str, Any]]] = []
    frontier, seen = [orgnr], {orgnr}
    for _ in range(CHAIN_DEPTH):
        level: list[dict[str, Any]] = []
        for of in frontier:
            # At most 100 / CHAIN_MIN_PCT holders qualify, so fetch them all and count here.
            sql = f"{CHAIN_HOLDERS} ORDER BY shares DESC, name"
            rows = [dict(r) for r in conn.execute(sql, (of, year, CHAIN_MIN_PCT))]
            level.extend({**_public(r), "of": of} for r in rows[:CHAIN_PER_LEVEL])
            if len(rows) > CHAIN_PER_LEVEL:
                level.append({"more": len(rows) - CHAIN_PER_LEVEL, "of": of})
        if not level:
            break
        levels.append(level)
        frontier = [o["orgnr"] for o in level if o.get("orgnr") and o["orgnr"] not in seen]
        seen.update(frontier)
    return levels


def _public(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": r["name"],
        "orgnr": r["orgnr"],
        "birth_year": r["birth_year"],
        "postcode": r["postcode"],
        "place": r["place"],
        "country": r["country"],
        "class": r["class"],
        "shares": r["shares"],
        "total": r["total"],
        "pct": pct(r["shares"], r["total"]),
    }


def _owns(conn: sqlite3.Connection, orgnr: str, year: int) -> tuple[list[dict[str, Any]], int]:
    rows = conn.execute(
        """SELECT h.orgnr, c.name, SUM(h.shares) AS shares, cc.total_shares AS total, sc.name AS class
        FROM holdings h JOIN companies c USING (year, orgnr)
        JOIN company_classes cc USING (year, orgnr, class_id)
        JOIN share_classes sc ON sc.id = h.class_id
        WHERE h.holder_orgnr = ? AND h.year = ? AND h.orgnr != ?
        GROUP BY h.orgnr, h.class_id ORDER BY c.name""",
        (orgnr, year, orgnr),
    ).fetchall()
    out = [{**dict(r), "pct": pct(r["shares"], r["total"])} for r in rows]
    return out[:MAX_OWNS], len(out)


def _owns_count(conn: sqlite3.Connection, orgnr: str) -> dict[int, int]:
    rows = conn.execute(
        "SELECT year, COUNT(DISTINCT orgnr) FROM holdings WHERE holder_orgnr = ? AND orgnr != ? "
        "GROUP BY year",
        (orgnr, orgnr),
    )
    return dict(rows.fetchall())


def _has_holders(conn: sqlite3.Connection, orgnr: str, year: int) -> bool:
    sql = "SELECT 1 FROM holdings WHERE orgnr = ? AND year = ? LIMIT 1"
    return conn.execute(sql, (orgnr, year)).fetchone() is not None


def _ultimate(conn: sqlite3.Connection, orgnr: str, year: int) -> dict[str, Any]:
    """Final owners: people, foreign holders and companies without owners in the register, with
    the stake each holds once every level is multiplied in.

    Stake is passed up one company at a time and summed where paths meet, so a company reached
    two ways passes on both (largest pending first, to merge as much as possible before passing
    on). Stake coming back to `orgnr` is like own shares and is dropped; a loop elsewhere is
    passed round until it fades out, ending with the owners outside it. `through` lists the
    companies all of an owner's stake came through, nearest the owner first."""
    pending: dict[str, tuple[float, list[str]]] = {orgnr: (100.0, [])}
    owners: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    fetched: dict[str, tuple[float, list[tuple[dict[str, Any], float]]]] = {}
    is_leaf: dict[str, bool] = {}

    def holders(of: str, threshold: float) -> list[tuple[dict[str, Any], float]]:
        """`of`'s holders with at least `threshold` %, fetched once at the lowest threshold asked."""
        got = fetched.get(of)
        if got is None or threshold < got[0]:
            rows = map(dict, conn.execute(CHAIN_HOLDERS, (of, year, threshold)))
            got = fetched[of] = (
                threshold,
                [(r, p) for r in rows if (p := pct(r["shares"], r["total"]))],
            )
        return [(r, p) for r, p in got[1] if p >= threshold]

    def meet(a: list[str] | None, b: list[str]) -> list[str]:
        return b if a is None else [c for c in a if c in b]

    for _ in range(ULTIMATE_MAX_STEPS):
        if not pending:
            break
        of = max(pending, key=lambda k: pending[k][0])
        stake, via = pending.pop(of)
        chain = via if of == orgnr else [*via, of]
        for r, p in holders(of, ULTIMATE_EPS * 100 / stake):
            held, h = stake * p / 100, r["orgnr"]
            if h == orgnr:
                continue
            if h:
                names[h] = r["name"]
                if h not in is_leaf:
                    is_leaf[h] = not _has_holders(conn, h, year)
                if not is_leaf[h]:
                    before, seen = pending.get(h, (0.0, None))
                    pending[h] = (before + held, meet(seen, chain))
                    continue
            o = owners.setdefault(holder_key(r), {**_public(r), "pct": 0.0, "via": None})
            o["pct"] += held
            o["via"] = meet(o["via"], chain)

    big = sorted(
        (o for o in owners.values() if o["pct"] >= ULTIMATE_MIN_PCT),
        key=lambda o: (-o["pct"], o["name"] or ""),
    )
    return {
        "owners": [
            {
                **{k: o[k] for k in ("name", "orgnr", "birth_year", "country")},
                "pct": round(o["pct"], 4),
                "through": [
                    {"orgnr": c, "name": names[c]} for c in dict.fromkeys(reversed(o["via"]))
                ],
            }
            for o in big[:ULTIMATE_MAX]
        ],
        "n_owners": len(big),
        "traced": round(sum(o["pct"] for o in big), 4),
    }


def _holds_counts(conn: sqlite3.Connection, orgnrs: set[str], year: int) -> dict[str, int]:
    """How many companies each of `orgnrs` holds shares in (those holding none are left out)."""
    keys, out = sorted(orgnrs), {}
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        out.update(
            conn.execute(
                f"SELECT holder_orgnr, COUNT(DISTINCT orgnr) FROM holdings WHERE year = ? "
                f"AND holder_orgnr IN ({','.join('?' * len(chunk))}) AND orgnr != holder_orgnr "
                "GROUP BY holder_orgnr",
                (year, *chunk),
            ).fetchall()
        )
    return out


def _below(
    edges: dict[str, list[tuple[str, float | None, str]]], order: list[str]
) -> dict[str, set[str]]:
    """The companies under each opened one along «own» edges, each once, itself left out."""
    below: dict[str, set[str]] = {}
    for of in reversed(order):  # deepest first, so most children are done when reached
        seen: set[str] = set()
        stack = [of]
        while stack:
            for c, _, kind in edges[stack.pop()]:
                if kind != "own" or c in seen:
                    continue
                seen.add(c)
                if c in below:
                    seen |= below[c]
                else:
                    stack.append(c)
        seen.discard(of)
        below[of] = seen
    return below


def _down(
    conn: sqlite3.Connection, orgnr: str, year: int, root_rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Everything `orgnr` holds, level by level, opening the companies `follows` lets through.
    `root_rows` (HOLDS-shaped) replace the first level's query, for a person at the root.

    Each company's holdings are fetched once, and every parent that holds it lists it, so the
    map can draw it whole in each place (a loop back to a company above is the map's to stop).
    Edges per parent are (company, stake, kind): «own» opened, «small» not opened. `under`
    counts the companies below each opened one, each once; `more` how many companies a «small»
    one holds, so the map can open it on request."""
    edges: dict[str, list[tuple[str, float | None, str]]] = {}
    names: dict[str, str] = {}
    depth, through = {orgnr: 0}, {orgnr: 100.0}
    order, truncated = [orgnr], False
    for of in order:  # grows while we iterate: breadth first
        edges[of] = []
        if of == orgnr and root_rows is not None:
            rows = list(root_rows)
        else:
            rows = [dict(r) for r in conn.execute(HOLDS, (of, year))]
        rows.sort(key=lambda r: (-(pct(r["shares"], r["total"]) or 0), r["name"]))
        for r in rows:
            c, p = r["orgnr"], pct(r["shares"], r["total"])
            names[c] = r["name"]
            eff = through[of] * (p or 0) / 100
            if not (p and follows(p, eff)):
                kind = "small"
            elif c in depth:
                kind = "own"
            elif depth[of] >= STRUCTURE_DEPTH or len(order) > STRUCTURE_MAX_COMPANIES:
                kind, truncated = "small", True
            else:
                kind = "own"
                depth[c], through[c] = depth[of] + 1, eff
                order.append(c)
            edges[of].append((c, p, kind))
    below = _below(edges, order)
    under = {of: len(b) for of, b in below.items()}
    rank = {"own": 0, "small": 1}
    for of in order:
        edges[of].sort(key=lambda e: (rank[e[2]], -under.get(e[0], 0), -(e[1] or 0), names[e[0]]))
    small = {c for e in edges.values() for c, _, kind in e if kind == "small"} - set(depth)
    return {
        "edges": {of: e for of, e in edges.items() if e},
        "names": names,
        "under": under,
        "below": below,
        "more": _holds_counts(conn, small, year),
        "companies": len(order) - 1,
        "depth": max(depth.values()),
        "small": sum(1 for e in edges.values() for _, _, kind in e if kind == "small"),
        "truncated": truncated,
    }


def _up(conn: sqlite3.Connection, orgnr: str, year: int) -> dict[str, Any]:
    """Owners with at least CHAIN_MIN_PCT, level by level, without the chain's per-level cap. An
    owner reached twice is climbed once; the map draws it with its owners in both places."""
    edges: dict[str, list[tuple[str, float | None, str]]] = {}
    nodes: dict[str, dict[str, Any]] = {}
    depth = {orgnr: 0}
    frontier = [orgnr]
    while frontier:
        nxt = []
        for of in frontier:
            sql = f"{CHAIN_HOLDERS} ORDER BY shares DESC, name"
            for r in map(dict, conn.execute(sql, (of, year, CHAIN_MIN_PCT))):
                key = holder_key(r)
                if key not in depth:
                    depth[key] = depth[of] + 1
                    nodes[key] = {k: r[k] for k in ("name", "orgnr", "birth_year", "country")}
                    if r["orgnr"] and depth[key] < STRUCTURE_DEPTH:
                        nxt.append(key)
                edges.setdefault(of, []).append((key, pct(r["shares"], r["total"]), "own"))
        frontier = nxt
    return {"edges": edges, "nodes": nodes, "owners": len(depth) - 1, "depth": max(depth.values())}


def _group(down: dict[str, Any], orgnr: str) -> dict[str, Any] | None:
    """The company page's summary of `_down`: the MAP_BRANCHES biggest direct holdings and the rest."""
    edges = down["edges"].get(orgnr, [])
    if not edges:
        return None

    def branch(e: tuple[str, float | None, str]) -> dict[str, Any]:
        c, p, kind = e
        return {"orgnr": c, "name": down["names"][c], "pct": p, "kind": kind,
                "under": down["under"][c] if kind == "own" else None}  # fmt: skip

    rest = edges[MAP_BRANCHES:]
    return {
        "direct": len(edges),
        "companies": down["companies"],
        "depth": down["depth"],
        "branches": [branch(e) for e in edges[:MAP_BRANCHES]],
        "rest": len(rest),
        "rest_under": len(set().union(*(down["below"][c] for c, _, kind in rest if kind == "own"))),
    }


def years() -> list[int]:
    with closing(_connect()) as conn:
        return _years(conn)


def _structure_payload(
    root: str, name: str | None, year: int, years: list[int], down: dict[str, Any],
    up: dict[str, Any] | None = None, root_node: dict[str, Any] | None = None,
) -> dict[str, Any]:  # fmt: skip
    up = up or {"edges": {}, "nodes": {}, "owners": 0, "depth": 0}
    nodes = {c: {"name": n, "orgnr": c} for c, n in down["names"].items()}
    nodes.update(up["nodes"])
    nodes[root] = root_node or {"name": name, "orgnr": root}
    return {
        "version": STRUCTURE_VERSION,
        "found": bool(down["edges"] or up["edges"]),
        "orgnr": root,
        "name": name,
        "year": year,
        "years": years,
        "rules": {"follow": FOLLOW_RULE, "below": FOLLOW_BELOW, "owner_pct": CHAIN_MIN_PCT},
        "nodes": nodes,
        "down": down["edges"],
        "under": down["under"],
        "more": down["more"],
        "up": up["edges"],
        "stats": {
            "companies": down["companies"],
            "down_depth": down["depth"],
            "small": down["small"],
            "truncated": down["truncated"],
            "owners": up["owners"],
            "up_depth": up["depth"],
        },
    }


def _pick_year(conn: sqlite3.Connection, year: int | None) -> tuple[int, list[int]]:
    all_years = _years(conn)
    if not all_years:
        raise Unavailable("register has no years")
    return (year if year in all_years else all_years[-1]), all_years


def structure(orgnr: str, year: int | None = None) -> dict[str, Any]:
    """The whole selskapsstruktur for one register year: owners up, holdings down, final owners."""
    with closing(_connect()) as conn:
        year, all_years = _pick_year(conn, year)
        name = conn.execute(
            "SELECT name FROM companies WHERE orgnr = ? ORDER BY year DESC LIMIT 1", (orgnr,)
        ).fetchone()
        down, up = _down(conn, orgnr, year), _up(conn, orgnr, year)
        ultimate = _ultimate(conn, orgnr, year)
    out = _structure_payload(orgnr, name[0] if name else None, year, all_years, down, up)
    return {**out, "ultimate": ultimate}


def person_key(name: str, birth_year: int | None) -> str:
    """A person's key in a structure, as holder_key makes it."""
    return f"{name}|{birth_year or ''}"


def _person_rows(
    conn: sqlite3.Connection, name: str, birth_year: int | None, year: int
) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(PERSON_HOLDS, (name, birth_year, year))]


def person_structure(name: str, birth_year: int | None, year: int | None = None) -> dict[str, Any]:
    """A person's eierstruktur for one register year: their holdings and everything below."""
    with closing(_connect()) as conn:
        year, all_years = _pick_year(conn, year)
        key = person_key(name, birth_year)
        down = _down(conn, key, year, _person_rows(conn, name, birth_year, year))
    node = {"name": name, "orgnr": None, "birth_year": birth_year}
    out = _structure_payload(key, name, year, all_years, down, root_node=node)
    return {**out, "kind": "person", "birth_year": birth_year}


def holdings_tree(orgnr: str, year: int) -> dict[str, Any]:
    """What one company holds, followed down by the usual rule: the map opens a stake under the
    line with this when asked to."""
    with closing(_connect()) as conn:
        year, _ = _pick_year(conn, year)
        down = _down(conn, orgnr, year)
    return {k: down[k] for k in ("edges", "names", "under", "more")} | {"year": year}


def snapshot(orgnr: str, year: int | None = None) -> dict[str, Any]:
    """Everything the Eierskap section shows for one register year (default: the latest)."""
    with closing(_connect()) as conn:
        years = _years(conn)
        if not years:
            raise Unavailable("register has no years")
        year = year if year in years else years[-1]
        n_rows = conn.execute("SELECT COUNT(*) FROM holdings WHERE orgnr = ?", (orgnr,)).fetchone()[
            0
        ]
        owns, owns_total = _owns(conn, orgnr, year)
        name = conn.execute(
            "SELECT name FROM companies WHERE orgnr = ? ORDER BY year DESC LIMIT 1", (orgnr,)
        ).fetchone()
        base = {
            "found": bool(n_rows or owns_total),
            "name": name[0] if name else None,
            "years": years,
            "year": year,
            "chain": _chain(conn, orgnr, year),
            "ultimate": _ultimate(conn, orgnr, year),
            "group": _group(_down(conn, orgnr, year), orgnr),
            "follow_rule": FOLLOW_RULE,
            "follow_below": FOLLOW_BELOW,
            "owns": owns,
            "owns_total": owns_total,
            "owns_count": _owns_count(conn, orgnr),
        }
        if n_rows > HISTORY_MAX_ROWS:
            return {**base, **_top_only(conn, orgnr, year)}
        hist = _load_history(conn, orgnr, years)
    holders = hist.year(year)
    i = years.index(year)
    gone = []
    if i:
        now = {(h.key, h.class_id) for h in holders}
        gone = [h.to_dict() for h in hist.year(years[i - 1]) if (h.key, h.class_id) not in now]
    n_changes = len(changes(hist))
    return {
        **base,
        "large": False,
        "classes": sorted({h.class_name for h in holders}),
        "holders": [{**h.to_dict(), "tag": _tag(hist, h, year)} for h in holders[:TOP_HOLDERS]],
        "holders_total": len(holders),
        "gone": gone,
        "n_changes": n_changes,
    }


def _top_only(conn: sqlite3.Connection, orgnr: str, year: int) -> dict[str, Any]:
    rows = conn.execute(
        f"SELECT {HOLDING_COLS} {HOLDING_FROM} WHERE h.orgnr = ? AND h.year = ? "
        "ORDER BY h.shares DESC LIMIT ?",
        (orgnr, year, TOP_HOLDERS),
    )
    total = conn.execute(
        "SELECT COUNT(*) FROM holdings WHERE orgnr = ? AND year = ?", (orgnr, year)
    ).fetchone()[0]
    holders = [{**_public(r), "tag": None} for r in map(dict, rows)]
    return {
        "large": True,
        "classes": sorted({h["class"] for h in holders}),
        "holders": holders,
        "holders_total": total,
        "gone": [],
        "n_changes": None,
    }


def history(orgnr: str) -> dict[str, Any]:
    """Holders × years matrix and the list of changes, for companies under HISTORY_MAX_ROWS."""
    with closing(_connect()) as conn:
        years = _years(conn)
        n_rows = conn.execute("SELECT COUNT(*) FROM holdings WHERE orgnr = ?", (orgnr,)).fetchone()[
            0
        ]
        if n_rows > HISTORY_MAX_ROWS:
            return {"years": years, "large": True, "rows": [], "changes": [], "rows_total": None}
        hist = _load_history(conn, orgnr, years)
    latest = years[-1]
    ordered = sorted(
        hist.by_key.values(),
        key=lambda by: (
            latest not in by,  # current holders first
            -max(pct(h.shares, h.total) or 0 for h in by.values()),
        ),
    )
    rows = []
    for by in ordered[:HISTORY_ROWS]:
        any_h = by.get(latest) or by[max(by)]
        rows.append(
            {
                **{
                    k: v
                    for k, v in any_h.to_dict().items()
                    if k in ("name", "orgnr", "birth_year", "class")
                },
                "by_year": {y: _cell(by, years, y) for y in years if y in by},
            }
        )
    all_changes = changes(hist)
    return {
        "years": years,
        "large": False,
        "rows": rows,
        "rows_total": len(ordered),
        "changes": all_changes[:HISTORY_CHANGES],
        "changes_total": len(all_changes),
    }


def _cell(by: dict[int, Holding], years: list[int], y: int) -> dict[str, Any]:
    h = by[y]
    i = years.index(y)
    prev = by.get(years[i - 1]) if i else None
    state = (
        None if i == 0 else "in" if prev is None else None if _same_share(prev, h) else "changed"
    )
    return {"shares": h.shares, "total": h.total, "pct": pct(h.shares, h.total), "state": state}


def person(name: str, birth_year: int | None, year: int | None = None) -> dict[str, Any]:
    """What one person (name + birth year; the register has no national ID) owns, by year."""
    with closing(_connect()) as conn:
        years = _years(conn)
        if not years:
            raise Unavailable("register has no years")
        year = year if year in years else years[-1]
        rows = conn.execute(
            """SELECT h.year, h.class_id, sc.name AS class, h.orgnr AS company, c.name AS company_name,
                h.holder_postcode AS postcode, h.holder_place AS place, h.country, h.shares,
                cc.total_shares AS total
            FROM holdings h JOIN company_classes cc USING (year, orgnr, class_id)
            JOIN share_classes sc ON sc.id = h.class_id JOIN companies c USING (year, orgnr)
            WHERE h.holder_name = ? AND h.holder_birth_year IS ? AND h.holder_orgnr IS NULL""",
            (name, birth_year),
        ).fetchall()
        key = person_key(name, birth_year)
        group = _group(_down(conn, key, year, _person_rows(conn, name, birth_year, year)), key)
    hist = History(years)
    latest: dict[str, Any] | None = None
    for r in map(dict, rows):
        by = hist.by_key.setdefault((r["company"], r["class_id"]), {})
        h = by.get(r["year"])
        if h is None:
            h = by[r["year"]] = Holding(
                r["company"], r["class_id"], r["company_name"], r["company"], None,
                r["postcode"], r["place"], r["country"], r["class"], total=r["total"],
            )  # fmt: skip
        h.shares += r["shares"]
        if latest is None or r["year"] > latest["year"]:
            latest = r
    # Share counts aren't comparable across companies; order by stake.
    holdings = sorted(hist.year(year), key=lambda h: (-(pct(h.shares, h.total) or 0), h.name or ""))
    i = years.index(year)
    gone = []
    if i:
        now = {(h.key, h.class_id) for h in holdings}
        gone = [h.to_dict() for h in hist.year(years[i - 1]) if (h.key, h.class_id) not in now]
    all_changes = changes(hist)
    return {
        "found": bool(rows),
        "name": name,
        "birth_year": birth_year,
        "address": {k: latest[k] for k in ("year", "postcode", "place", "country")}
        if latest
        else None,
        "years": years,
        "year": year,
        "holdings": [{**h.to_dict(), "tag": _tag(hist, h, year)} for h in holdings],
        "gone": gone,
        "counts": {y: len({h.key for h in hist.year(y)}) for y in years},
        "group": group,
        "follow_rule": FOLLOW_RULE,
        "follow_below": FOLLOW_BELOW,
        "changes": all_changes[-HISTORY_CHANGES:],
        "n_changes": len(all_changes),
    }

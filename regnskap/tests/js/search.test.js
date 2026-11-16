// Ranking of the top search (src/static/search.js). Run with `bun test`.
import { describe, expect, test } from "bun:test";
import { COMPANIES_WITH_PEOPLE, matchScore, searchGroups } from "../../src/static/search.js";

const company = (name) => ({ name, orgnr: "910000000" });
const person = (name) => ({ name, birth_year: 1970 });
const names = (groups) => groups.map((g) => [g.kind, g.hits.map((h) => h.name)]);

describe("match score", () => {
  test("the name itself, every word, or less", () => {
    expect(matchScore("OLA HAUG", "Ola  haug")).toBe(3);
    expect(matchScore("OLA ARNE HAUGEN", "ola haug")).toBe(2);
    expect(matchScore("HAUG", "ola haug")).toBe(1);
    expect(matchScore("OLAS SKATESERVICE OLA KJERNET", "ola haug")).toBe(1);
  });

  test("Norwegian letters and punctuation", () => {
    expect(matchScore("BJØRN ÅSE", "bjø åse")).toBe(2);
    expect(matchScore("BJØRN ÅSE", "bjo ase")).toBe(1);
    expect(matchScore("ANNE-LISE BERG", "anne lise berg")).toBe(3);
    expect(matchScore("ORKLA ASA", "orkla")).toBe(2);
  });
});

describe("groups", () => {
  // What Brreg and the register return for «ola haug» (the screenshot that started this).
  const brreg = ["OLAS SKATESERVICE OLA KJERNET", "HAUG", "OLA AGNALT", "OLA ANDERSEN",
    "OLA BAKKEN", "OLA BAKKER", "OLA BERG", "OLA DAHL"].map(company);

  test("a person named exactly what was typed comes before loose company hits", () => {
    const groups = searchGroups(brreg, [person("OLA HAUGEN"), person("OLA HAUG")], "ola haug");
    expect(names(groups)[0]).toEqual(["person", ["OLA HAUG", "OLA HAUGEN"]]);
    expect(groups[1]).toMatchObject({ kind: "company", more: true });
    expect(groups[1].hits).toHaveLength(COMPANIES_WITH_PEOPLE);
  });

  test("companies stay first when they match as well", () => {
    const groups = searchGroups([company("NMK ORKLA"), company("ORKLA ASA")], [person("PER ORKLA HANSEN")], "orkla");
    expect(names(groups)).toEqual([["company", ["NMK ORKLA", "ORKLA ASA"]], ["person", ["PER ORKLA HANSEN"]]]);
    expect(names(searchGroups([company("ORKLA")], [person("PER ORKLA HANSEN")], "orkla"))[0][0]).toBe("company");
  });

  test("within a group, full matches first; otherwise the order they came in", () => {
    const groups = searchGroups([company("HAUG"), company("OLA HAUG AS"), company("OLA BAKKEN")], [], "ola haug");
    expect(names(groups)).toEqual([["company", ["OLA HAUG AS", "HAUG", "OLA BAKKEN"]]]);
    expect(groups[0].more).toBe(false); // nothing cut without people
  });

  test("more people than came: the group ends with a row for all of them", () => {
    const karis = Array.from({ length: 6 }, () => person("KARI HANSEN"));
    const [first] = searchGroups([company("KARI HANSEN AS")], karis, " kari hansen ", { total: 23 });
    expect(first.kind).toBe("person");
    expect(first.hits.at(-1)).toEqual({ kind: "people-all", q: "kari hansen", total: 23, capped: false, score: 0 });
    expect(searchGroups([], karis, "kari", { total: 1000, capped: true })[0].hits.at(-1)).toMatchObject({ total: 1000, capped: true });
    expect(searchGroups([], karis, "kari", { total: 6 })[0].hits.map((h) => h.kind)).not.toContain("people-all");
    expect(searchGroups([], karis, "kari")[0].hits).toHaveLength(6); // no total given: just what came
  });

  test("an empty group is left out", () => {
    expect(names(searchGroups([], [person("KARI NORDMANN")], "kari"))).toEqual([["person", ["KARI NORDMANN"]]]);
    expect(searchGroups([], [], "x")).toEqual([]);
  });
});

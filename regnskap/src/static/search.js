// Order of the top search's hits: companies from Brreg and people from the shareholder register.
// Brreg's name search also returns names with only some of the words typed («ola haug» →
// OLA AGNALT, HAUG), while the person search needs them all; so each hit is scored by how well
// its name answers the search, and the group with the best hit comes first. No DOM here; app.js
// draws the groups and tests/js/search.test.js runs this under bun.

// With people in the list, companies stop here, so both groups show without scrolling.
export const COMPANIES_WITH_PEOPLE = 6;

const words = (t) => (t || "").toLocaleUpperCase("nb").split(/[^\p{L}\p{N}]+/u).filter(Boolean);

/** 3: the name is what was typed. 2: every word typed starts a word in the name. 1: less. */
export function matchScore(name, q) {
  const want = words(q), have = words(name);
  if (!want.length) return 1;
  if (have.join(" ") === want.join(" ")) return 3;
  return want.every((w) => have.some((h) => h.startsWith(w))) ? 2 : 1;
}

/**
 * The dropdown's groups in order: [{kind, hits, more}], `hits` best first (a stable sort, so ties
 * keep the order they came in) and `more` true when companies were cut to make room for people.
 * People come first only when their best hit beats the companies' best. When more people match
 * than came (`total`, `capped` past the server's count limit), the people end with a
 * {kind: "people-all"} hit that opens the full list.
 */
export function searchGroups(companies, people, q, { total = people.length, capped = false } = {}) {
  const scored = (list, kind) => list.map((h) => ({ ...h, kind, score: matchScore(h.name, q) })).sort((a, b) => b.score - a.score);
  const firms = scored(companies, "company"), persons = scored(people, "person");
  const cut = persons.length ? COMPANIES_WITH_PEOPLE : firms.length;
  const companyGroup = { kind: "company", hits: firms.slice(0, cut), more: firms.length > cut };
  const all = persons.length && (capped || total > persons.length) ? [{ kind: "people-all", q: q.trim(), total, capped, score: 0 }] : [];
  const personGroup = { kind: "person", hits: [...persons, ...all], more: false };
  const best = (g) => (g.hits.length ? g.hits[0].score : 0);
  const groups = best(personGroup) > best(companyGroup) ? [personGroup, companyGroup] : [companyGroup, personGroup];
  return groups.filter((g) => g.hits.length);
}

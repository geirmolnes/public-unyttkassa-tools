// Formatting (and small list helpers) shared by app.js and the eierkart layout (eierkart.js).

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
export const nf0 = new Intl.NumberFormat("nb-NO", { maximumFractionDigits: 0 });
const nfPct = new Intl.NumberFormat("nb-NO", { maximumFractionDigits: 2 });

export const fmtPct = (p) => {
  if (p === null || p === undefined) return "–";
  return p > 0 && p < 0.01 ? "< 0,01 %" : `${nfPct.format(p)} %`;
};
/** Amount in 1 000 units with a real minus sign: 1 054 321 → "1 054", -1 054 321 → "−1 054", null → "–". */
export function fmtThousands(v) {
  if (v === null || v === undefined) return "–";
  // "|| 0": a small negative rounds to -0, which formats as "-0".
  return nf0.format(Math.round(v / 1000) || 0).replace("-", "−");
}
export const fmtInt = (n) => (n === null || n === undefined ? "–" : nf0.format(n));
/** "1 eier", "2 eiere". */
export const plural = (n, one, many) => `${fmtInt(n)} ${n === 1 ? one : many}`;

// Norwegian org numbers start with 8 or 9. The register gives foreign holders 9-digit IDs from
// 2xx xxx xxx, which Brreg doesn't know, so those aren't links.
export const norwegian = (orgnr) => /^[89]\d{8}$/.test(orgnr || "");

/** Recently seen companies with `company` moved to the front, once. */
export const recentFirst = (recent, company) =>
  [{ orgnr: company.orgnr, name: company.name }, ...recent.filter((r) => r.orgnr !== company.orgnr)];

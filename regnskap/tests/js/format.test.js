// Number formatting shared by app.js and eierkart.js (src/static/format.js). Run with `bun test`.
import { expect, test } from "bun:test";
import { fmtThousands, recentFirst } from "../../src/static/format.js";

const NBSP = " ";

test.each([
  ["thousands with a group separator", 1054321, `1${NBSP}054`],
  ["real minus sign", -1054321, `−1${NBSP}054`],
  ["rounds half up", 1500, "2"],
  ["just under half rounds down", 1499, "1"],
  // Regression: a cost of 0 is negated to -0 for display and showed as "−0" in the all-years table.
  ["negative zero", -0, "0"],
  ["a small negative is 0, not −0", -31, "0"],
  ["negative half rounds toward zero", -500, "0"],
  ["zero", 0, "0"],
  ["missing", null, "–"],
  ["undefined", undefined, "–"],
])("%s", (_, v, expected) => {
  expect(fmtThousands(v)).toBe(expected);
});

// Nylig sett: the open company goes on top (it used to be hidden from the list), once.
const A = { orgnr: "111111111", name: "A AS" }, B = { orgnr: "222222222", name: "B AS" }, C = { orgnr: "333333333", name: "C AS" };

test.each([
  ["empty list", [], A, [A]],
  ["new company on top", [B, C], A, [A, B, C]],
  ["already on top stays once", [A, B], A, [A, B]],
  ["from the middle to the top", [B, A, C], A, [A, B, C]],
  ["from the bottom to the top", [B, C, A], A, [A, B, C]],
  ["only payload fields kept", [B], { ...A, roles: [], place: "OSLO" }, [A, B]],
])("recentFirst: %s", (_, recent, company, expected) => {
  expect(recentFirst(recent, company)).toEqual(expected);
});

test("recentFirst leaves the list it was given alone", () => {
  const recent = [B, A];
  recentFirst(recent, A);
  expect(recent).toEqual([B, A]);
});

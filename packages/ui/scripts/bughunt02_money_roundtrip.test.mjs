// BUGHUNT-02 C8 — vendored from the coordinator probe
// (coordonare/rapoarte/coordonator/BUGHUNT-BOAGENTS-20261003/reproduced/money_roundtrip.mjs).
// Runs under `npm test` (node --test on Node 22); assertions identical to the
// coordinator harness, only wrapped in test() and re-pathed to ../src/lib.
import assert from "node:assert/strict";
import test from "node:test";
import { formatMoney, parseMoneyInput, moneyInputValue, parseMoneyNumber, money } from "../src/lib/money-format.ts";

test("C8 1234,56 round-trips as 1.234,56 lei", () => {
  const saved = parseMoneyInput("1.234,56");
  assert.equal(saved, "1234.56");
  assert.equal(moneyInputValue(saved), "1.234,56");
  assert.equal(formatMoney(saved, "RON", 2), "1.234,56 lei");
  assert.equal(formatMoney(saved, "EUR", 2), "1.234,56 €");
});

test("C8 1,005 is rejected at input and rendered visibly invalid, never silently 1,01", () => {
  // F3/D07 (BO01 patch): over-precision must not be saved nor silently
  // rounded on display. parseMoneyInput refuses; money() shows the
  // original value marked invalid.
  assert.throws(() => parseMoneyInput("1,005"), /mai mult de 2 zecimale/);
  assert.throws(() => parseMoneyNumber("1,005"));
  assert.throws(() => formatMoney("1.005", "RON", 2), /mai mult de 2 zecimale/);
  assert.equal(money("1.005", "RON", 2), "1.005 — Sumă cu mai mult de 2 zecimale");
});

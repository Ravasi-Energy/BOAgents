// BUGHUNT-02 C8 — vendored from the coordinator probe
// (coordonare/rapoarte/coordonator/BUGHUNT-BOAGENTS-20261003/reproduced/money_roundtrip.mjs).
// Runs under `npm test` (node --test on Node 22); assertions identical to the
// coordinator harness, only wrapped in test() and re-pathed to ../src/lib.
import assert from "node:assert/strict";
import test from "node:test";
import { formatMoney, parseMoneyInput, moneyInputValue, parseMoneyNumber } from "../src/lib/money-format.ts";

test("C8 1234,56 round-trips as 1.234,56 lei", () => {
  const saved = parseMoneyInput("1.234,56");
  assert.equal(saved, "1234.56");
  assert.equal(moneyInputValue(saved), "1.234,56");
  assert.equal(formatMoney(saved, "RON", 2), "1.234,56 lei");
  assert.equal(formatMoney(saved, "EUR", 2), "1.234,56 €");
});

test("C8 1,005 keeps three decimals in editor, formats 1,01 at two", () => {
  const three = parseMoneyInput("1,005");
  assert.equal(three, "1.005");
  assert.equal(moneyInputValue(three), "1,005");
  assert.equal(formatMoney(three, "RON", 2), "1,01 lei");
  const asNumber = parseMoneyNumber("1,005");
  assert.equal(typeof asNumber, "number");
  assert.equal(moneyInputValue(asNumber), "1,005");
});

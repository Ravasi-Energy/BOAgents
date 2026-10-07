import test from "node:test";
import assert from "node:assert/strict";
import { formatMoney, money } from "../src/lib/money-format.ts";
for (const [currency, label] of [["RON", "lei"], ["EUR", "€"], ["USD", "USD"], ["JPY", "JPY"]]) {
  for (const decimals of [0, 2]) test(`${currency}, decimals=${decimals}`, () => {
    assert.equal(formatMoney(6000, currency, decimals), `6.000${decimals ? ",00" : ""} ${label}`);
    assert.equal(formatMoney("6000", currency, decimals), formatMoney(6000, currency, decimals));
  });
}
test("negative control rejects hidden division by 100 and incorrect currency", () => {
  const contract = formatter => assert.equal(formatter(6000, "EUR", 2), "6.000,00 €");
  contract(formatMoney);
  assert.throws(() => contract((amount, currency, decimals) => formatMoney(amount / 100, currency, decimals)), assert.AssertionError);
  assert.throws(() => contract((amount, _currency, decimals) => formatMoney(amount, "RON", decimals)), assert.AssertionError);
});
test("precision, rounding, invalid data", () => {
  assert.equal(formatMoney("1000000000000000000000", "EUR", 0), "1.000.000.000.000.000.000.000 €");
  assert.throws(() => formatMoney(1e21, "EUR", 0), /precizie sigură/);
  assert.equal(formatMoney("12345678901234567890.12", "EUR", 2), "12.345.678.901.234.567.890,12 €");
  assert.equal(formatMoney("-999.99", "USD", 2), "-999,99 USD");
  assert.equal(formatMoney(0, "RON", 0), "0 lei");
  // F3/D07: sub-cent and over-precision values are never silently rounded —
  // formatMoney refuses, money() renders the original marked invalid.
  for (const amount of [1e-7, "-999.995", "1.005"]) assert.throws(() => formatMoney(amount, "USD", 2), /mai mult de 2 zecimale/);
  assert.equal(money("-999.995", "USD", 2), "-999.995 — Sumă cu mai mult de 2 zecimale");
  for (const currency of [null, "", "eur"]) assert.throws(() => formatMoney(6000, currency, 2));
  for (const amount of [NaN, Infinity, "6.000,00", null]) assert.throws(() => formatMoney(amount, "EUR", 2));
  assert.throws(() => formatMoney(6000, "RON", 1));
});

import { parseMoneyInput, moneyInputValue, parseMoneyNumber } from "../src/lib/money-format.ts";

test("Romanian input/save/reload preserves major units, currency and editable precision", () => {
  for (const currency of ["RON", "EUR", "USD"]) for (const decimals of [0, 2]) {
    const saved = JSON.parse(JSON.stringify({ amount: parseMoneyInput("6.000,25"), currency }));
    assert.equal(saved.amount, "6000.25");
    assert.equal(moneyInputValue(saved.amount), "6.000,25");
    assert.equal(formatMoney(saved.amount, saved.currency, decimals), formatMoney("6000.25", currency, decimals));
  }
  assert.equal(parseMoneyInput(moneyInputValue("12345678901234567890.12")), "12345678901234567890.12");
  assert.equal(parseMoneyNumber("6.000,25"), 6000.25);
  assert.throws(() => parseMoneyNumber("12.345.678.901.234.567.890,12"));
  assert.equal(parseMoneyInput(""), null);
});
test("Romanian editor rejects ambiguous grouping and precision loss", () => {
  for (const text of ["6.00,25", "6,000.25", "6000.25", "6.000,", "6.000,25 EUR", "NaN"]) assert.throws(() => parseMoneyInput(text));
  const contract = parser => assert.equal(parser("6.000,25"), "6000.25");
  contract(parseMoneyInput);
  assert.throws(() => contract(text => String(parseFloat(text))), assert.AssertionError);
});

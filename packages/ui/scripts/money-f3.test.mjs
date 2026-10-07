import test from "node:test";
import assert from "node:assert/strict";
import { formatMoney, parseMoneyInput, moneyInputValue, moneyCurrency } from "../src/lib/money-format.ts";
for (const currency of ["RON", "EUR", "USD", "GBP"]) for (const decimals of [0, 2]) {
 test(`F3 ${currency} ${decimals}: major units, exact persisted string`, () => {
  const raw = parseMoneyInput("6.000,25");
  assert.equal(raw, "6000.25");
  assert.equal(moneyInputValue(raw), "6.000,25");
  const label = currency === "RON" ? "lei" : currency === "EUR" ? "€" : currency;
  assert.equal(formatMoney(raw, currency, decimals), `6.000${decimals ? ",25" : ""} ${label}`);
  assert.notEqual(formatMoney(raw, currency, decimals), formatMoney("60.00", currency, decimals));
  assert.equal(formatMoney("0", currency, decimals), `0${decimals ? ",00" : ""} ${label}`);
  assert.equal(formatMoney("-6000.25", currency, decimals), `-6.000${decimals ? ",25" : ""} ${label}`);
 });
}
test("F3 third fraction refused independently of magnitude/display precision", () => {
 for (const raw of ["1.005", "-999.995", "12345678901234567890.123", 1e-7]) for (const decimals of [0, 2]) assert.throws(() => formatMoney(raw, "USD", decimals), /zecimale/);
 for (const raw of ["1,005", "12.345.678.901.234.567.890,123"]) assert.throws(() => parseMoneyInput(raw), /zecimale/);
});
test("F3 ARR exact string beyond Number range survives JSON and editor readback", () => {
 const value = parseMoneyInput("12.345.678.901.234.567.890,12");
 assert.equal(value, "12345678901234567890.12");
 assert.equal(JSON.parse(JSON.stringify({annual_revenue_arr:value})).annual_revenue_arr,value);
 assert.equal(moneyInputValue(value), "12.345.678.901.234.567.890,12");
 assert.equal(formatMoney(value, "EUR", 2), "12.345.678.901.234.567.890,12 €");
 assert.throws(() => formatMoney(Number(value), "EUR", 2), /precizie/);
});
test("F3 unknown/missing ISO code never becomes RON", () => {
 for (const code of [null,"","eur","ZZZ","XXX"]) assert.throws(() => formatMoney("6000",code,2), /Valut/);
 assert.throws(() => moneyCurrency("ZZZ"), /Valut/);
 assert.equal(moneyCurrency("GBP"), "GBP");
});

import {requiredMoneyInput} from "../src/lib/money-format.ts";
test("F3 mandate/run budget editor never sends empty/third-fraction or float",()=>{ assert.equal(requiredMoneyInput("6.000,25"),"6000.25");for(const value of ["","1,005"])assert.throws(()=>requiredMoneyInput(value));assert.equal(JSON.parse(JSON.stringify({budget_limit:requiredMoneyInput("12.345.678.901.234.567.890,12")})).budget_limit,"12345678901234567890.12"); });

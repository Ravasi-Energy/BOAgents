// Stefan monetary rule v2.0 supersedes contradictory F3 presentation assertions only.
import test from "node:test";
import assert from "node:assert/strict";
import { formatMoney, parseMoneyInput, moneyInputValue, moneyCurrency } from "../src/lib/money-format.ts";
for (const currency of ["RON", "EUR", "USD", "GBP"]) for (const decimals of [0, 2]) {
 test(`F3 ${currency} ${decimals}: major units, exact persisted string`, () => {
  const raw = parseMoneyInput("6.000,25");
  assert.equal(raw, "6000.25");
  assert.equal(moneyInputValue(raw), "6.000,25");
  const label = currency === "RON" ? "lei" : currency === "EUR" ? "€" : currency;
  assert.equal(formatMoney(raw, currency, decimals), decimals ? `6.000,25 ${label}` : `≈ 6.000 ${label} — Valoare exactă: 6.000,25 ${label}`);
  assert.notEqual(formatMoney(raw, currency, decimals), formatMoney("60.00", currency, decimals));
  assert.equal(formatMoney("0", currency, decimals), `0${decimals ? ",00" : ""} ${label}`);
  assert.equal(formatMoney("-6000.25", currency, decimals), decimals ? `-6.000,25 ${label}` : `≈ -6.000 ${label} — Valoare exactă: -6.000,25 ${label}`);
 });
}
test("v2 display precision differs from explicit operation precision", () => {
 for (const raw of ["1.005", "-999.995", "12345678901234567890.123"]) for (const decimals of [0, 2]) assert.match(formatMoney(raw,"USD",decimals), /^≈ .* — Valoare exactă: /);
 assert.throws(()=>formatMoney(1e-7,"USD",2), /precizie/);
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
test("F3 mandate/run budget editor never sends empty/third-fraction or float",()=>{ assert.equal(requiredMoneyInput("6.000,25","EUR"),"6000.25");for(const value of ["","0","-1","1.000.000,001"])assert.throws(()=>requiredMoneyInput(value,"EUR"));assert.equal(requiredMoneyInput("1.000.000,00","EUR"),"1000000.00");assert.equal(requiredMoneyInput("1,234","KWD"),"1.234");assert.throws(()=>requiredMoneyInput("12.345.678.901.234.567.890,12","EUR")); });

import {serverMoney} from '../src/lib/money-server.ts';
test('F3 authoritative server0/2, scope/role/version, missing/invalid never fallback2',()=>{for(const value of [0,2])assert.deepEqual(serverMoney({tenant:'alpha',role:'viewer',settings:[{key:'bo.ui.money_display_decimals',value,version:3}]}),{tenant:'alpha',role:'viewer',value,version:3});for(const data of [null,{}, {tenant:'alpha',role:'admin',settings:[]},{tenant:'alpha',role:'admin',settings:[{key:'bo.ui.money_display_decimals',value:1,version:3}]},{tenant:'alpha',role:'admin',settings:[{key:'bo.ui.money_display_decimals',value:0,version:-1}]}])assert.throws(()=>serverMoney(data));});

// Current mandate/run routes lack currency; v2 blocks the dependent monetary write until owner schema.
test("v2 execution monetary input refuses absent/invalid currency before request",()=>{for(const currency of [undefined,null,"","ZZZ"])assert.throws(()=>requiredMoneyInput("6.000,00",currency),/Valut/);});

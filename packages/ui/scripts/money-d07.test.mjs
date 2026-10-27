// D07: amounts are major units with the ISO currency carried by the data, shown ro-RO with the 0 or 2
// decimals of the server setting. 6000 is "6.000", never "60" (no /100), and a missing currency is a
// visible diagnostic, never "lei". D07_MONEY_MODULE swaps the formatter so a mutant (e.g. /100) must
// make this file fail.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

const target = process.env.D07_MONEY_MODULE ? pathToFileURL(path.resolve(process.env.D07_MONEY_MODULE)).href : "../src/lib/money-format.ts";
const { formatMoney, money } = await import(target);
const SRC = path.resolve(new URL("../src", import.meta.url).pathname);
const read = (p) => fs.readFileSync(path.join(SRC, p), "utf8");

const LABEL = { RON: "lei", EUR: "€", USD: "USD" };
// [amount from data, decimals from Settings, expected integer part, expected fraction at 2 decimals]
const MATRIX = [
  ["6000", "6.000", "00"], [6000, "6.000", "00"], ["6000.00", "6.000", "00"],
  ["140000000", "140.000.000", "00"], ["0", "0", "00"], ["1234567.89", "1.234.568|1.234.567", "89"],
];

for (const currency of ["RON", "EUR", "USD"]) {
  test(`D07 ${currency}: major units, ro-RO grouping, 0 and 2 decimals from Settings`, () => {
    const label = LABEL[currency];
    for (const [amount, int, frac] of MATRIX) {
      const [int0, int2 = int0] = int.split("|");
      assert.equal(formatMoney(amount, currency, 2), `${int2},${frac} ${label}`, `${amount} ${currency} 2`);
      const zero = formatMoney(amount, currency, 0);
      if (frac === "00") assert.equal(zero, `${int0} ${label}`, `${amount} ${currency} 0`);
      else assert.equal(zero, `≈ ${int0} ${label} — Valoare exactă: ${int2},${frac} ${label}`);
    }
    assert.equal(formatMoney("-6000.25", currency, 2), `-6.000,25 ${label}`);
    assert.equal(formatMoney("-6000", currency, 0), `-6.000 ${label}`);
  });
}

test("D07 negative probe: 6000 never renders as 60 (no /100, no minor units)", () => {
  for (const currency of ["RON", "EUR", "USD"]) for (const decimals of [0, 2]) {
    const out = formatMoney("6000", currency, decimals);
    assert.match(out, /^6\.000(,00)? /, `${currency} ${decimals}: ${out}`);
    assert.doesNotMatch(out, /^60(,00)? /, `${currency} ${decimals}: ${out}`);
  }
});

test("D07 the currency comes from the data: missing or invalid is a diagnostic, never lei", () => {
  for (const currency of [null, undefined, "", "lei", "ron", "XXX", "ZZZ"]) {
    const out = money("140000000", currency, 2);
    assert.match(out, /^140000000 — Valută necunoscută/, String(currency));
    assert.doesNotMatch(out, /lei|RON|€|USD/, String(currency));
  }
  for (const decimals of [1, 3, -1, NaN]) assert.throws(() => formatMoney("6000", "RON", decimals), /0 sau 2/);
  // Floats that cannot prove their decimal are refused, not rounded silently.
  assert.match(money(6000.1, "EUR", 2), /fără precizie/);
});

test("D07 consumers pass the data's currency field, no literal RON/lei/EUR fallback", () => {
  const uses = {
    "components/company-profile/ProfileSections.tsx": [/<Money amount=\{profile\.annual_revenue_arr\} currency=\{profile\.annual_revenue_arr_currency\} \/>/, /<Money amount=\{profile\.financials\.burn_rate_monthly\} currency=\{profile\.financials\.burn_rate_currency\} \/>/],
    "app/demo/page.tsx": [/formatARR\(fx\.arr, fx\.arr_currency\)/, /<Money amount=\{arr\} currency=\{currency\} \/>/],
    "app/clients/page.tsx": [/<Money amount=\{c\.retainer_amount\} currency=\{c\.retainer_currency\} \/>/],
  };
  for (const [file, patterns] of Object.entries(uses)) for (const p of patterns) assert.match(read(file), p, file);
  const files = [];
  const walk = (d) => { for (const e of fs.readdirSync(d, { withFileTypes: true })) { const p = path.join(d, e.name); if (e.isDirectory()) walk(p); else if (/\.tsx?$/.test(e.name)) files.push(p); } };
  walk(SRC);
  const literal = files.filter((f) => /<Money[^>]*currency=["'{]\s*["']?(RON|EUR|lei)["']/.test(fs.readFileSync(f, "utf8"))).map((f) => path.relative(SRC, f));
  assert.deepEqual(literal, [], "a Money with a hard-coded currency masks the data");
  const code = read("lib/money-format.ts").replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
  assert.doesNotMatch(code, /\/\s*100\b|\*\s*0\.01/, "no minor-unit scaling in the formatter");
});

test("D07 decimals come from the server setting; a missing setting is not assumed to be 2", async () => {
  const { serverMoney, MONEY_KEY } = await import("../src/lib/money-server.ts");
  const row = (value) => ({ settings: [{ key: MONEY_KEY, value, version: 1 }], role: "viewer", tenant: "firma-a" });
  assert.equal(serverMoney(row(0)).value, 0);
  assert.equal(serverMoney(row(2)).value, 2);
  for (const bad of [row(1), row(null), row("2"), { settings: [], role: "viewer", tenant: "firma-a" }, null]) {
    assert.throws(() => serverMoney(bad), /nu se presupun 2 zecimale|nu a putut fi citit/);
  }
  assert.match(read("lib/Money.tsx"), /money\(amount,currency,row\.value\)/);
});

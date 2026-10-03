import assert from "node:assert/strict";
import { formatMoney, parseMoneyInput, moneyInputValue, parseMoneyNumber } from "../../packages/ui/src/lib/money-format.ts";

const saved = parseMoneyInput("1.234,56");
assert.equal(saved, "1234.56");
assert.equal(moneyInputValue(saved), "1.234,56");
assert.equal(formatMoney(saved, "RON", 2), "1.234,56 lei");
assert.equal(formatMoney(saved, "EUR", 2), "1.234,56 €");
console.log("C8 1234,56 round-trip afisat=" + formatMoney(saved, "RON", 2));

const three = parseMoneyInput("1,005");
assert.equal(three, "1.005");
assert.equal(moneyInputValue(three), "1,005");
const shown = formatMoney(three, "RON", 2);
console.log(
  "C8 1,005 parse=" + three + " editor=" + moneyInputValue(three) +
    " format-2zecimale=" + shown,
);
assert.equal(shown, "1,01 lei");
const asNumber = parseMoneyNumber("1,005");
console.log("C8 parseMoneyNumber(1,005)=" + asNumber + " type=" + typeof asNumber);
assert.equal(typeof asNumber, "number");
assert.equal(moneyInputValue(asNumber), "1,005");

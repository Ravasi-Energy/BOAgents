import test from 'node:test';import assert from 'node:assert/strict';
import {formatMoney,money,parseMoneyInput,moneyInputValue} from '../src/lib/money-format.ts';
const cases=[['6000','RON',0,'6.000 lei'],['6000','RON',2,'6.000,00 lei'],['6000','EUR',0,'6.000 €'],['6000','EUR',2,'6.000,00 €'],['6000','USD',0,'6.000 USD'],['6000','USD',2,'6.000,00 USD'],['6000','JPY',0,'6.000 JPY'],['6000','JPY',2,'6.000,00 JPY'],['60','RON',2,'60,00 lei'],['0.05','RON',2,'0,05 lei'],['-6000','EUR',2,'-6.000,00 €'],['0','USD',2,'0,00 USD'],['6000.25','EUR',0,'≈ 6.000 € — Valoare exactă: 6.000,25 €'],['1.234','KWD',2,'≈ 1,23 KWD — Valoare exactă: 1,234 KWD']];
for(const [a,c,d,want] of cases)test(`v2 ${a} ${c} ${d} complete output`,()=>assert.equal(formatMoney(a,c,d),want));
test('v2 exact half-up ties both signs, visible exact, negative zero',()=>{
 for(const [a,want] of [['1.235','≈ 1,24 KWD — Valoare exactă: 1,235 KWD'],['-1.235','≈ -1,24 KWD — Valoare exactă: -1,235 KWD'],['-0.000','0,00 KWD'],['-0.005','≈ -0,01 KWD — Valoare exactă: -0,005 KWD']])assert.equal(formatMoney(a,'KWD',2),want);
 assert.equal(formatMoney('-2.5','EUR',0),'≈ -3 € — Valoare exactă: -2,5 €');
 assert.equal(formatMoney('6000.000','USD',2),'6.000,00 USD');
});
test('v2 missing/invalid amount/currency never fallback zero/RON; fractional numbers fail exact contract',()=>{
 for(const c of [null,'','eur','ZZZ','XXX'])assert.throws(()=>formatMoney('6000',c,2));
 for(const a of [null,undefined,'',NaN,Infinity,1.234,9007199254740992,'6.000,00','1e3'])assert.throws(()=>formatMoney(a,'EUR',2));
 assert.match(money(null,'EUR',2),/sumă absentă/);
});
test('v2 Romanian editor exact roundtrip with explicit operation precision/limit',()=>{
 const stored={amount:parseMoneyInput('6.000,00'),currency:'EUR'};assert.deepEqual(stored,{amount:'6000.00',currency:'EUR'});
 assert.equal(parseMoneyInput(moneyInputValue(stored.amount)),stored.amount);
 const kwd={maxScale:3,maxIntegerDigits:12};assert.equal(parseMoneyInput('1,234',kwd),'1.234');assert.equal(moneyInputValue('1.234'),'1,234');
 assert.equal(parseMoneyInput('999.999.999.999,999',kwd),'999999999999.999');assert.throws(()=>parseMoneyInput('1.000.000.000.000,000',kwd));assert.throws(()=>parseMoneyInput('1,2345',kwd));
 for(const x of ['6.00,00','6,000.00','6000.00','6.000,','6.000,00 EUR','1,2,3','1 000,00','NaN','0.000,10'])assert.throws(()=>parseMoneyInput(x));
 assert.equal(parseMoneyInput(''),null);
});
test('v2 exact large decimal no Number intermediary',()=>assert.equal(formatMoney('999999999999999999999999999999.125','KWD',2),'≈ 999.999.999.999.999.999.999.999.999.999,13 KWD — Valoare exactă: 999.999.999.999.999.999.999.999.999.999,125 KWD'));

test('v2 unsafe legacy numeric editor is explicit diagnostic, not render crash or authoritative decimal',()=>{const text=moneyInputValue(1.234);assert.match(text,/legacy fără precizie exactă/);assert.throws(()=>parseMoneyInput(text));});

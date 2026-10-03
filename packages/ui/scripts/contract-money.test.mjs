import test from 'node:test';
import assert from 'node:assert/strict';
import {retainerMoneyPatch, moneyCurrency, parseMoneyNumber, formatMoney} from '../src/lib/money-format.ts';
for (const currency of ['RON','EUR','USD']) test(`retainer exact ${currency} JSON roundtrip`,()=>{
 const patch=retainerMoneyPatch('6.000,25',currency);
 assert.deepEqual(JSON.parse(JSON.stringify(patch)),{retainer_amount:'6000.25',retainer_currency:currency});
 assert.equal(formatMoney(patch.retainer_amount,patch.retainer_currency,2),`6.000,25 ${currency==='RON'?'lei':currency==='EUR'?'€':'USD'}`);
 assert.equal(parseMoneyNumber('6.000,25'),6000.25);
});
test('legacy unknown stays unknown; no default currency',()=>{assert.equal(moneyCurrency(''),null);assert.deepEqual(retainerMoneyPatch('',''),{retainer_amount:null,retainer_currency:null});});
test('partial retainer and ambiguous currency refused',()=>{
 for(const [amount,currency] of [['6.000,25',''],['','EUR'],['6000','usd'],['6000','EURO']]) assert.throws(()=>retainerMoneyPatch(amount,currency));
});
test('string retainer preserves precision beyond JSON numbers',()=>{
 assert.equal(retainerMoneyPatch('12.345.678.901.234.567.890,1234','EUR').retainer_amount,'12345678901234567890.1234');
 assert.throws(()=>parseMoneyNumber('12.345.678.901.234.567.890,1234'));
});

test('final backend retainer constraints and unsupported clear are explicit',()=>{
 for(const value of ['-1','1,1234567','12345678901234567890123456789012345']) assert.throws(()=>retainerMoneyPatch(value,'EUR'));
 assert.throws(()=>retainerMoneyPatch('','',true),/nu este disponibilă/);
 assert.equal(parseMoneyNumber('-1'),-1); // existing ARR/burn numeric contract permits negatives
});

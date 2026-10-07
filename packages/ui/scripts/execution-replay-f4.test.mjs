import test from 'node:test';import assert from 'node:assert/strict';import {replayPayload} from '../src/lib/execution-replay.ts';
const p={mandate_id:'m',steps:[{action:'increment',resource:'synth.counter',payload:{amount:1}}],budget_amount:'5',correlation_id:'same-intent'};
test('B-P1-1 uncertain replay retains the same correlation and complete intent',()=>{assert.equal(replayPayload(null,p),p);assert.deepEqual(replayPayload(p,structuredClone(p)),p);});
test('B-P1-1 changed amount/steps/mandate/correlation refused before HTTP',()=>{for(const patch of[{budget_amount:'10'},{steps:[{action:'increment',resource:'synth.counter',payload:{amount:2}}]},{mandate_id:'other'},{correlation_id:'new'}])assert.throws(()=>replayPayload(p,{...p,...patch}),/Draftul a fost modificat/);});
test('B-P1-1 JSON key order is not a changed intent',()=>{assert.deepEqual(replayPayload(p,{...p,steps:[{payload:{amount:1},resource:'synth.counter',action:'increment'}]}).steps[0].payload,{amount:1});});

import {parseRunDraft} from '../src/lib/execution-replay.ts';
test('B-P1-1 persisted uncertain intent restores exact strings and key, corrupt storage refused',()=>{assert.deepEqual(parseRunDraft(JSON.stringify(p)),p);for(const value of [null,[],{}, {...p,budget_amount:5},{...p,steps:[]},{...p,correlation_id:''}])assert.throws(()=>parseRunDraft(JSON.stringify(value)));});

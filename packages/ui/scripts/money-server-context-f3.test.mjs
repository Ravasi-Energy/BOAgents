import test from 'node:test';import assert from 'node:assert/strict';import {moneyEpoch} from '../src/lib/money-event.ts';import {activateClient,loadFixture,unloadFixture} from '../src/lib/api.ts';
test('F3 actual context transport: invalidation only after confirmed JSON success, denied write has no event',async(t)=>{
 const calls=[];let release;const initial=moneyEpoch();
 t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url,method:options.method});return {ok:true,json:()=>new Promise(resolve=>release=resolve)}});
 const activation=activateClient('alpha/beta?');await new Promise(resolve=>setImmediate(resolve));assert.equal(moneyEpoch(),initial);release({slug:'alpha/beta?'});await activation;assert.equal(moneyEpoch(),initial+1);assert.equal(calls[0].url,'/api/backend/clients/alpha%2Fbeta%3F/activate');assert.equal(calls[0].method,'POST');
 globalThis.fetch=async()=>new Response(JSON.stringify({detail:'forbidden'}),{status:403});await assert.rejects(()=>loadFixture('synthetic'),/forbidden/);assert.equal(moneyEpoch(),initial+1);
 globalThis.fetch=async()=>new Response('<html/>',{status:200});await assert.rejects(()=>unloadFixture());assert.equal(moneyEpoch(),initial+1);
 globalThis.fetch=async()=>new Response(JSON.stringify({unloaded:true}),{status:200});await unloadFixture();assert.equal(moneyEpoch(),initial+2);
});

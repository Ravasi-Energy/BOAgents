import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import vm from 'node:vm';
const require=createRequire(import.meta.url);
const ts=require('typescript');
const source=readFileSync(new URL('../src/app/signin/page.tsx',import.meta.url),'utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
function page(session,decision){const calls=[];const module={exports:{}};vm.runInNewContext(code,{module,exports:module.exports,require:(id)=>id==='@/auth'?{auth:async()=>session,checkEmailAllowed:async email=>{calls.push(email);return decision;},signIn:async()=>{}}:id==='next/navigation'?{redirect:dest=>{throw new Error('REDIRECT '+dest);}}:require(id)});return{render:()=>module.exports.default({searchParams:Promise.resolve({callbackUrl:'/people'})}),calls};}
test('Unexpired but revoked session renders sign-in instead of redirect loop',async()=>{const p=page({user:{email:'OPERATOR@bo01.invalid'}},{allowed:false,rosterUnknown:false});const tree=await p.render();assert.equal(tree.type,'main');assert.deepEqual(p.calls,['operator@bo01.invalid']);});
test('Unknown roster fails closed on sign-in without redirect',async()=>{const p=page({user:{email:'admin@bo01.invalid'}},{allowed:false,rosterUnknown:true});assert.equal((await p.render()).type,'main');});
test('Active authorized session redirects to its safe destination',async()=>{const p=page({user:{email:'admin@bo01.invalid'}},{allowed:true});await assert.rejects(p.render(),/REDIRECT \/people/);assert.deepEqual(p.calls,['admin@bo01.invalid']);});
test('No session does not call roster revalidation',async()=>{const p=page(null,{allowed:false});assert.equal((await p.render()).type,'main');assert.deepEqual(p.calls,[]);});

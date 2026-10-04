// Wire observations of the existing contract. No new delta/delete DTO.
// Empty-list observations are NOT safety PASS. Page draft regression is separate.
import {test,after} from 'node:test';
import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';
import path from 'node:path';
const source=process.env.BO_UI_SOURCE_DIR || path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../src');
const bo=await import(pathToFileURL(path.join(source,'lib/bo.ts')));
const api=await import(pathToFileURL(path.join(source,'lib/api.ts')));
const originalFetch=globalThis.fetch;after(()=>{globalThis.fetch=originalFetch});
let wire=[];global.fetch=async(url,options={})=>{wire.push({url,method:options.method||'GET',body:options.body});return {ok:true,status:200,json:async()=>({}),text:async()=>''}};
function body(){return JSON.parse(wire.at(-1).body)}
test('omitted undefined fields do not become empty lists in bot transport',async()=>{await bo.patchBoBot('synthetic',{expected_version:2,name:'draft',content:undefined});assert.deepEqual(body(),{expected_version:2,name:'draft'});});
test('current profile transport preserves caller scalar delta without adding other fields',async()=>{const patch={financials:{runway_months:24}};await api.updateCompanyProfile(patch);assert.deepEqual(body(),patch);assert.deepEqual(patch,{financials:{runway_months:24}});});
test('catalog omitted lists remain absent at serialization boundary',async()=>{await bo.updateBoCatalogEntry('synthetic',{provider:'synthetic',model_id:'model',purpose:'changed',source:'admin',expected_version:2});assert(!('capabilities' in body()));assert(!('regions' in body()));});
test('people omitted departments remain absent in transport',async()=>{await api.updatePerson(1,{full_name:'Changed'});assert.deepEqual(body(),{full_name:'Changed'});});
test('explicit empty CSV is observed unchanged; NOT acceptance of clearing',async()=>{await bo.putBoSetting('bo.router.allowed_providers','',2);assert.deepEqual(body(),{value:'',expected_version:2});});
test('explicit empty bot list is observed; unsafe semantic remains baseline defect',async()=>{await bo.patchBoBot('synthetic',{expected_version:2,content:{steps:[]}});assert.deepEqual(body().content.steps,[]);});
test('existing department DELETE uses only explicit slug and DELETE method',async()=>{await api.deleteDepartment('doi');assert.equal(wire.at(-1).method,'DELETE');assert.equal(wire.at(-1).url,'/api/backend/departments/doi');assert.equal(wire.at(-1).body,undefined);});
test('409 is propagated by real BO transport and input draft object is not mutated',async()=>{const draft={expected_version:2,name:'keep draft',content:{steps:[{id:'s4'}]}};const copy=structuredClone(draft);global.fetch=async()=>({ok:false,status:409,json:async()=>({error:'version_conflict',detail:'stale'})});await assert.rejects(bo.patchBoBot('synthetic',draft),e=>e instanceof bo.BoApiError&&e.status===409);assert.deepEqual(draft,copy);});

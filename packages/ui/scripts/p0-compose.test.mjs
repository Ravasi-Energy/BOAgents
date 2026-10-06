import test from 'node:test';import assert from 'node:assert/strict';import {pathToFileURL,fileURLToPath} from 'node:url';
const root=process.env.BOAGENTS_R12_SOURCE||fileURLToPath(new URL('../src',import.meta.url));
const {settingPatch,profileDelta}=await import(pathToFileURL(`${root}/lib/p0-edit.ts`));
const {personEdit,personPatch,rebasePersonEdit}=await import(pathToFileURL(`${root}/lib/person-edit.ts`));
const {putBoSetting}=await import(pathToFileURL(`${root}/lib/bo.ts`));
const {updateCompanyProfile,updatePerson}=await import(pathToFileURL(`${root}/lib/api.ts`));
async function intercepted(run){const old=globalThis.fetch;const calls=[];globalThis.fetch=async(path,init)=>{calls.push({path,body:JSON.parse(init.body)});return new Response('{"error":"conflict"}',{status:409})};try{await run(calls)}finally{globalThis.fetch=old}}
test('B-P0-1 CSV builder through real PUT: empty stays no-op; explicit remove survives409',()=>intercepted(async calls=>{
 const base={version:2,value:'a,b,c'};for(const [draft,remove,expected]of [['','',{value:'',expected_version:2}],['a,b,c,d','b',{value:'d',expected_version:2,remove:['b']}]] ){
 const p=settingPatch('bo.router.allowed_providers',draft,base,remove);await assert.rejects(putBoSetting('bo.router.allowed_providers',p.value,p.expected_version,p.remove),e=>e.status===409);assert.deepEqual(calls.at(-1).body,expected);assert.deepEqual(base,{version:2,value:'a,b,c'});
 }
}));
test('B-P0-2 nested trust editor accepts corrected union/no-op contract; scalar 20→10 wire is genuine intent',()=>intercepted(async calls=>{
 const base={version:2,value:JSON.stringify({policyVersion:20,publishers:[{publisherId:'p1',allowedProducts:['Guardian','Hire']}]})};
 for(const products of [[],['Guardian'],['Guardian','Hire','BOAgents']])assert.doesNotThrow(()=>settingPatch('bo.packages.trust_store_json',JSON.stringify({policyVersion:20,publishers:[{publisherId:'p1',allowedProducts:products}]}),base));
 assert.equal(calls.length,0);
 const p=settingPatch('bo.packages.trust_store_json',JSON.stringify({policyVersion:10,publishers:[]}),base);await assert.rejects(putBoSetting('bo.packages.trust_store_json',p.value,p.expected_version),e=>e.status===409);assert.deepEqual(calls[0].body,{value:'{"policyVersion":10}',expected_version:2});
}));
test('B-P0-5/6 profile builder through PATCH: no implicit vendors clear or financial defaults',()=>intercepted(async calls=>{
 const base={name:'Synthetic',vendors:['a','b','c'],financials:{burn_rate_monthly:6000,burn_rate_currency:'EUR',runway_months:20,key_metrics:{clients:2}}};
 const draft={...structuredClone(base),vendors:[],financials:{...base.financials,runway_months:10}};
 const before=structuredClone(draft);await assert.rejects(updateCompanyProfile(profileDelta(draft,base)));assert.deepEqual(calls[0].body,{financials:{runway_months:10}});assert.deepEqual(draft,before);
}));
test('People builder+PATCH409+explicit rebase keeps local20→10 and remote name, explicit contact clear',()=>intercepted(async calls=>{
 const base={id:7,version:1,full_name:'Synthetic',role:'Viewer',email:'synthetic@bo01.invalid',preferred_channel:'email',response_sla_hours:20,authority_scope:[],availability:[],on_leave_until:'2030-01-01'};
 const draft={...personEdit(base),response_sla_hours:'10',on_leave_until:''};const before=structuredClone(draft);
 await assert.rejects(updatePerson(7,personPatch(draft,base)),e=>e.status===409);assert.deepEqual(calls[0].body,{expected_version:1,response_sla_hours:10,clear_on_leave:true});assert.deepEqual(draft,before);
 const fresh={...base,version:4,full_name:'Remote',role:'Admin'};const rebased=rebasePersonEdit(draft,base,fresh);await assert.rejects(updatePerson(7,personPatch(rebased,fresh)),e=>e.status===409);assert.deepEqual(calls[1].body,{expected_version:4,response_sla_hours:10,clear_on_leave:true});assert.equal(rebased.full_name,'Remote');assert.equal(rebased.role,'Admin');
 assert.equal(personPatch({...draft,email:''},base).clear_email,true);assert.equal(calls.length,2);
}));

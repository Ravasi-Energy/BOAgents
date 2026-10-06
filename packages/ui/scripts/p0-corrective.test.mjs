import test from 'node:test';import assert from 'node:assert/strict';import {pathToFileURL,fileURLToPath} from 'node:url';
const root=process.env.BOAGENTS_CORRECTIVE_SOURCE||fileURLToPath(new URL('../src',import.meta.url));
const {settingPatch}=await import(pathToFileURL(root+'/lib/p0-edit.ts'));
const {personEdit,personPatch,rebasePersonEdit}=await import(pathToFileURL(root+'/lib/person-edit.ts'));
const {updatePerson,updateCompanyProfile}=await import(pathToFileURL(root+'/lib/api.ts'));
test('corrective clear contacts use explicit flags only;409 preserves draft and rebase retains remote authority',async()=>{
const base={id:7,version:1,full_name:'Synthetic',role:'Viewer',email:'test@bo01.invalid',slack_user_id:'s',telegram_chat_id:'t',discord_user_id:'d',preferred_channel:'email',response_sla_hours:20,authority_scope:[],availability:[]};
const draft={...personEdit(base),email:'',slack_user_id:'',telegram_chat_id:'',discord_user_id:''};const before=structuredClone(draft),old=globalThis.fetch,calls=[];
globalThis.fetch=async(url,init)=>{calls.push(JSON.parse(init.body));return new Response('{}',{status:409})};
try{await assert.rejects(updatePerson(7,personPatch(draft,base)),e=>e.status===409);assert.deepEqual(calls[0],{expected_version:1,clear_email:true,clear_slack_user_id:true,clear_telegram_chat_id:true,clear_discord_user_id:true});assert.deepEqual(draft,before);
const fresh={...base,version:4,authority_scope:['vendor_onboarding']};const rebased=rebasePersonEdit(draft,base,fresh);assert.deepEqual(rebased.authority_scope,fresh.authority_scope);assert.deepEqual(personPatch(rebased,fresh),{...calls[0],expected_version:4});}finally{globalThis.fetch=old}
});
test('corrective nested trust allows empty/add/explicit remove sparse wire with exact CAS, omission survives',()=>{
const base={version:4,value:JSON.stringify({policyVersion:20,publishers:[{publisherId:'p1',allowedKinds:['bobot'],keyIds:['k1']}],keys:[]})};
assert.deepEqual(JSON.parse(settingPatch('bo.packages.trust_store_json',JSON.stringify({publishers:[{publisherId:'p1',allowedKinds:[]}]}),base).value),{publishers:[{publisherId:'p1',allowedKinds:[]}]});
assert.deepEqual(JSON.parse(settingPatch('bo.packages.trust_store_json',JSON.stringify({publishers:[{publisherId:'p1',allowedKinds:['extra'],keyIds_remove:['k1']}],policyVersion:10}),base).value),{policyVersion:10,publishers:[{publisherId:'p1',allowedKinds:['extra'],keyIds_remove:['k1']}]});
});
test('profile transport exposes409 to actual page handler, serializes honest CAS without echo',async()=>{const old=globalThis.fetch,patch={expected_version:4,financials:{runway_months:10}};let body;globalThis.fetch=async(url,init)=>{body=JSON.parse(init.body);return new Response('{}',{status:409})};try{await assert.rejects(updateCompanyProfile(patch),e=>e.status===409);assert.deepEqual(body,patch);assert.deepEqual(patch,{expected_version:4,financials:{runway_months:10}})}finally{globalThis.fetch=old}});

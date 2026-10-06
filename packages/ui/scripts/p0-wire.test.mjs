import test from 'node:test';
import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';
const root=process.env.BOAGENTS_R12_SOURCE || fileURLToPath(new URL('../src',import.meta.url));
const bo=await import(pathToFileURL(`${root}/lib/bo.ts`));
const api=await import(pathToFileURL(`${root}/lib/api.ts`));
// Hard-coded transport oracles. These test the actual exported UI clients,
// not a reconstruction of fetch or a simulated database implementation.
export const cases=[
 {id:'CALL-656ad5ffe3ff3e65b1',method:'PATCH',path:'/bo/bots/bot-synthetic',body:{expected_version:2,name:'Local',steps_remove:['s2']},response:{bot:{id:'bot-synthetic',draft_version:3}},call:b=>bo.patchBoBot('bot-synthetic',b)},
 {id:'CALL-aba5527b0a27d12476',method:'POST',path:'/bo/bots/bot-synthetic/publish',body:{expected_version:2},response:{result:'published',active_version_no:1},call:b=>bo.publishBoBot('bot-synthetic',b.expected_version)},
 {id:'CALL-6090cec84695c54fa3',method:'PUT',path:'/bo/routing/catalog/entry-synthetic',body:{provider:'synthetic',model_id:'m1',purpose:'test',source:'test',expected_version:2,regions_remove:['r2']},response:{entry:{entry_id:'entry-synthetic',version:3}},call:b=>bo.updateBoCatalogEntry('entry-synthetic',b)},
 {id:'CALL-1332014e0612f058ac',method:'POST',path:'/bo/routing/catalog',body:{provider:'synthetic',model_id:'m1',purpose:'test',source:'test'},response:{entry:{entry_id:'new',version:1}},call:b=>bo.createBoCatalogEntry(b)},
 {id:'CALL-1449444e023f102ea5',method:'PATCH',path:'/company-profile',body:{financials:{runway_months:10},vendors_remove:['VendorB']},response:{name:'Synthetic',financials:{burn_rate_monthly:6000,runway_months:10,burn_rate_currency:'EUR'}},call:b=>api.updateCompanyProfile(b)},
 {id:'CALL-360a5cab1b04075270',method:'DELETE',path:'/departments/synthetic',response:undefined,call:()=>api.deleteDepartment('synthetic')},
 {id:'CALL-c73ac4cae80fa621a6',method:'POST',path:'/departments',body:{title:'Synthetic',mission:'Fixture'},response:{config:{slug:'synthetic'}},call:b=>api.createDepartment(b)},
 {id:'CALL-e4c017968bdef52e6f',method:'PATCH',path:'/people/7',body:{expected_version:2,full_name:'Local',clear_on_leave:true},response:{id:7,version:3,full_name:'Local',on_leave_until:null},call:b=>api.updatePerson(7,b)},
 {id:'CALL-21b2427503e74a226b',method:'POST',path:'/people',body:{full_name:'Synthetic',role:'Viewer'},response:{id:8,full_name:'Synthetic'},call:b=>api.createPerson(b)},
 {id:'CALL-be87fa63a32f843d2f',method:'PUT',path:'/bo/settings/bo.router.allowed_providers',body:{value:'synthetic-new',expected_version:2,remove:['synthetic-old']},response:{result:'ok',applied:true,setting:{version:3}},call:b=>bo.putBoSetting('bo.router.allowed_providers',b.value,b.expected_version,b.remove)},
 {id:'CALL-c9692973e9cc6ba228',method:'POST',path:'/documents',multipart:true,response:{filename:'synthetic.txt',chunks_indexed:1,domain:'general'},call:()=>api.uploadDocument(new File(['synthetic text only'],'synthetic.txt',{type:'text/plain'}))},
 {id:'CALL-debae20ef752be0361',method:'POST',path:'/onboard/interview/commit',body:{session_id:'reviewed',profile:{name:'Synthetic'},people:[],departments:[]},response:{name:'Synthetic'},call:b=>api.commitOnboardDraft(b.session_id,b.profile,b.people,b.departments)},
];
for(const c of cases)test(`${c.id} actual wire, response, denial/conflict keep input and no retry`,async()=>{
 const fetchBefore=globalThis.fetch;const input=structuredClone(c.body);const draft=structuredClone(c.body);let requests=[];
 try{
  for(const status of [c.method==='DELETE'?204:200,403,409]){
   requests=[];
   globalThis.fetch=async(path,init)=>{requests.push({path,init});return new Response(status===204?null:JSON.stringify(status>=400?{error:'conflict',detail:'fixture refusal'}:c.response),{status})};
   if(status<400)assert.deepEqual(await c.call(input),c.response);else await assert.rejects(c.call(input),e=>c.path.startsWith('/bo/')||c.path==='/people/7'?e.status===status:true);
   assert.equal(requests.length,1);const {path,init}=requests[0];assert.equal(path,`/api/backend${c.path}`);assert.equal(init.method,c.method);
   if(c.multipart){assert.equal(init.headers,undefined);assert.ok(init.body instanceof FormData);assert.equal(await init.body.get('file').text(),'synthetic text only');assert.equal(init.body.get('domain'),'general');}
   else if(c.body!==undefined)assert.deepEqual(JSON.parse(init.body),c.body);else assert.equal(init.body,undefined);
   assert.deepEqual(input,draft);
  }
 }finally{globalThis.fetch=fetchBefore;}
});

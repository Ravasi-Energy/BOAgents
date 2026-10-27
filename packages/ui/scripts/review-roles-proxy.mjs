import test from'node:test';import assert from'node:assert/strict';import fs from'node:fs';import path from'node:path';import{pathToFileURL}from'node:url';import http from'node:http';import{spawn,execFileSync}from'node:child_process';
const{encode}=process.env.BO_READ_ERROR_NEXTAUTH?await import(pathToFileURL(process.env.BO_READ_ERROR_NEXTAUTH)):await import('next-auth/jwt');const output=path.resolve(process.env.BO_READ_ERROR_OUTPUT||'review-roles-results');fs.mkdirSync(output+'/tmp',{recursive:true});process.env.TMPDIR=output+'/tmp';
const PY=process.env.BO_CORE_PYTHON||path.resolve('../core/.venv/bin/python'),NEXT=process.env.BO_NEXT_BIN||'node_modules/next/dist/bin/next',SECRET=process.env.AUTH_SECRET||'BO01-synthetic-jwt-secret-minimum-32-characters',CORE=path.resolve('../core'),DB=output+'/episodic.db',DB2=output+'/bo_agents.db',SVC='svc-secret-test-32-chars-minimum-xx',PXY='proxy-secret-test-32-characters';
test('F02 review mutations through real Next proxy and real backend',{timeout:300000},async()=>{const out={pin:process.env.BO01_REVIEW_SHA,checks:[],observations:[],limits:['Synthetic viewer/admin JWT through real authjs session cookie, real Next proxy and real FastAPI/SQLite; no GoogleOAuth/workers/providers','Role matrix covers every review mutation; persistent audit and readback proven across a backend restart; no browser rendering claim']};
async function freePort(){const s=http.createServer();await new Promise((ok,no)=>{s.once('error',no);s.listen(0,'127.0.0.1',ok);});const p=s.address().port;await new Promise(ok=>s.close(ok));return p;}
function py(code,args=[]){return execFileSync(PY,['-c',code,...args],{env:{...process.env,EPISODIC_DB_PATH:DB},cwd:CORE,encoding:'utf8'});}
function kill(proc){if(proc&&proc.exitCode===null&&proc.signalCode===null){try{process.kill(-proc.pid,'SIGTERM');}catch{try{proc.kill('SIGTERM');}catch{}}}}
async function ready(url,proc){for(let i=0;i<240;i++){if(proc.exitCode!==null)throw new Error('process exited early: '+url);try{await fetch(url);return;}catch{await new Promise(ok=>setTimeout(ok,250));}}throw new Error('timeout waiting for '+url);}
let backend,next,bp,np;try{
fs.rmSync(DB,{force:true});fs.rmSync(DB+'-journal',{force:true});fs.rmSync(DB2,{force:true});fs.rmSync(DB2+'-journal',{force:true});
py("import os,sys;db=sys.argv[1];os.environ['EPISODIC_DB_PATH']=db;from openexecutive.knowledge.review_store import ContentType,ReviewStore;ReviewStore.initialize_db(db);s=ReviewStore(db_path=db);s.register('external:f02:firma-doc',ContentType.EXTERNAL,'f02','f02-firma.pdf');print('seeded')",[DB]);
bp=await freePort();const bfd=fs.openSync(output+'/backend.log','w');backend=spawn(PY,['-m','uvicorn','openexecutive.api.main:app','--host','127.0.0.1','--port',String(bp)],{env:{...process.env,EPISODIC_DB_PATH:DB,BOAGENTS_DB_PATH:DB2,EXEC_EMAIL_ADDRESS:'exec@firma-a.test',ANTHROPIC_API_KEY:'sk-test-not-used',BO_TENANT_ID:'firma-a',BO_ADMIN_EMAILS:'admin@firma-a.test',BACKEND_SHARED_SECRET:SVC,BACKEND_PROXY_SECRET:PXY},stdio:['ignore',bfd,bfd],detached:true});fs.closeSync(bfd);await ready('http://127.0.0.1:'+bp+'/health',backend);
np=await freePort();const O='http://127.0.0.1:'+np,nfd=fs.openSync(output+'/next.log','w');next=spawn(process.execPath,[NEXT,'start','--hostname','127.0.0.1','--port',String(np)],{env:{...process.env,AUTH_SECRET:SECRET,AUTH_URL:O,AUTH_TRUST_HOST:'true',ALLOWED_EMAILS:'viewer@firma-a.test,admin@firma-a.test',BACKEND_BASE_URL:'http://127.0.0.1:'+bp,BACKEND_SHARED_SECRET:SVC,BACKEND_PROXY_SECRET:PXY},stdio:['ignore',nfd,nfd],detached:true});fs.closeSync(nfd);await ready(O+'/signin',next);
const jwt=async email=>encode({token:{sid:crypto.randomUUID(),email,sub:email},secret:SECRET,salt:'authjs.session-token',maxAge:3600}),V=await jwt('viewer@firma-a.test'),A=await jwt('admin@firma-a.test');
const api=async(p,{email,method='GET',body}={})=>{const r=await fetch(O+'/api/backend'+p,{method,headers:{...(email?{cookie:'authjs.session-token='+email}:{}),...(body?{'content-type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});let j=null;try{j=await r.json();}catch{}return{status:r.status,json:j};};
const direct=async(p,{method='GET',body,headers={}}={})=>{const r=await fetch('http://127.0.0.1:'+bp+p,{method,headers:{...(body?{'content-type':'application/json'}:{}),...headers},...(body?{body:JSON.stringify(body)}:{})});let j=null;try{j=await r.json();}catch{}return{status:r.status,json:j};};
const audit=()=>JSON.parse(py("import json,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;print(json.dumps([dict(r) for r in c.execute(\"select event_type,actor,summary from audit_log where event_type='review_mutation_denied' order by ts\")]))",[DB]).trim()||'[]');
const item=async email=>{const r=await api('/review/items/'+encodeURIComponent('external:f02:firma-doc'),{email});assert.equal(r.status,200);return r.json.item;};
const itemId='external:f02:firma-doc',esc=encodeURIComponent(itemId);
// No session at all: the proxy gate itself refuses before any role question.
assert.equal((await api('/review/items')).status,401);out.checks.push({id:'f02-proxy-no-session-401',status:'PASS'});
// Viewer reads through the real session+proxy+backend chain.
const listed=await api('/review/items?status=pending&limit=500',{email:V});assert.equal(listed.status,200);assert(listed.json.some(x=>x.item_id===itemId));out.checks.push({id:'f02-viewer-read-200-proxy',status:'PASS'});
const before=await item(A);
// Every review mutation as viewer through the proxy: refused, store untouched, journaled.
const mutations=[
 ['PATCH item','PATCH','/review/items/'+esc,{priority:'high'}],
 ['bulk-approve','POST','/review/bulk-approve',{all_pending:true}],
 ['curate','POST','/review/curate',{domain:'f02',action:'stop'}],
 ['annotation create','POST','/review/items/'+esc+'/annotations',{correction:'viewer must not write'}],
 ['annotation patch','PATCH','/review/annotations/999999',{correction:'x'}],
 ['annotation delete','DELETE','/review/annotations/999999',null],
];
let denied=0;for(const[name,method,p,body]of mutations){const r=await api(p,{email:V,method,body});assert.equal(r.status,403,name);assert.deepEqual(await item(A),before,name+' changed the store');denied++;}
out.checks.push({id:'f02-viewer-every-mutation-403-store-untouched',status:'PASS',mutations:denied});
const journaled=audit();assert.equal(journaled.length,denied);assert(journaled.every(x=>x.actor==='viewer@firma-a.test'));out.checks.push({id:'f02-denials-persisted-audit-log-real-actor',status:'PASS',rows:journaled.length});
// Service key alone, direct to the backend, is not a delegated admin either.
assert.equal((await direct('/review/items/'+esc,{method:'PATCH',body:{priority:'low'},headers:{'x-api-key':SVC}})).status,403);
assert.equal((await direct('/review/items/'+esc,{method:'PATCH',body:{priority:'low'},headers:{'x-caller-email':'admin@firma-a.test'}})).status,401);assert.deepEqual(await item(A),before);out.checks.push({id:'f02-service-key-and-lone-email-refused',status:'PASS'});
// Tenant hint cannot pull a caller across installs; generic refusal, no oracle.
assert.equal((await api('/review/items?tenant=firma-b',{email:A})).status,403);out.checks.push({id:'f02-tenant-hint-mismatch-403',status:'PASS'});
// Admin through the same chain performs the same mutations for real.
assert.equal((await api('/review/items/'+esc,{email:A,method:'PATCH',body:{priority:'high'}})).status,200);assert.equal((await item(V)).priority,'high');out.checks.push({id:'f02-admin-patch-200-viewer-readback',status:'PASS'});
const ann=await api('/review/items/'+esc+'/annotations',{email:A,method:'POST',body:{correction:'admin annotation'}});assert.equal(ann.status,200);const annId=ann.json.annotation_id??ann.json.id;
assert.equal((await api('/review/annotations/'+annId,{email:A,method:'PATCH',body:{correction:'corrected'}})).status,200);assert.equal((await api('/review/annotations/'+annId,{email:A,method:'DELETE'})).status,200);out.checks.push({id:'f02-admin-annotation-lifecycle-200',status:'PASS'});
assert.equal((await api('/review/curate',{email:A,method:'POST',body:{domain:'f02',action:'stop'}})).status,200);
const bulk=await api('/review/bulk-approve',{email:A,method:'POST',body:{item_ids:[itemId]}});assert.equal(bulk.status,200);assert(bulk.json.item_ids.includes(itemId));assert.equal((await item(V)).status,'approved');out.checks.push({id:'f02-admin-curate-bulk-200-approved-readback',status:'PASS'});
// Persistence proof: kill the backend entirely, restart on the same SQLite.
kill(backend);await new Promise(ok=>setTimeout(ok,500));backend=null;
const bfd2=fs.openSync(output+'/backend-restart.log','w');backend=spawn(PY,['-m','uvicorn','openexecutive.api.main:app','--host','127.0.0.1','--port',String(bp)],{env:{...process.env,EPISODIC_DB_PATH:DB,BOAGENTS_DB_PATH:DB2,EXEC_EMAIL_ADDRESS:'exec@firma-a.test',ANTHROPIC_API_KEY:'sk-test-not-used',BO_TENANT_ID:'firma-a',BO_ADMIN_EMAILS:'admin@firma-a.test',BACKEND_SHARED_SECRET:SVC,BACKEND_PROXY_SECRET:PXY},stdio:['ignore',bfd2,bfd2],detached:true});fs.closeSync(bfd2);await ready('http://127.0.0.1:'+bp+'/health',backend);
const after=await item(A);assert.equal(after.status,'approved');assert.equal(after.priority,'high');
assert.equal(audit().length,denied+1);out.checks.push({id:'f02-state-and-denials-survive-backend-restart',status:'PASS'});
}catch(e){out.failure=String(e.stack);throw e;}finally{kill(next);kill(backend);fs.writeFileSync(output+'/RESULT.json',JSON.stringify(out,null,2));}});

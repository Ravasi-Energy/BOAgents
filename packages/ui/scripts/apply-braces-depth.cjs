'use strict';
// Downstream MIT braces3.0.3 patch. All preimages are checked before mutation.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const arg=process.argv.find(a=>a.startsWith('--app='));
const app=arg?path.resolve(arg.slice(6)):path.resolve(__dirname,'..');
const check=process.argv.includes('--check');
const sha=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const fail=message=>{throw new Error('BRACES_DEPTH_PATCH: '+message);};
try {
 const manifest=JSON.parse(fs.readFileSync(path.join(app,'patches/braces-depth.json'),'utf8'));
 const expected=['lib/compile.js','lib/expand.js','lib/parse.js','lib/security-depth.js','lib/stringify.js'];
 if(manifest.package!=='braces'||manifest.version!=='3.0.3'||
    JSON.stringify(Object.keys(manifest.files).sort())!==JSON.stringify(expected))fail('unexpected patch manifest');
 const root=path.join(app,'node_modules/braces');
 if(!fs.existsSync(root)) {
  const lock=JSON.parse(fs.readFileSync(path.join(app,'package-lock.json'),'utf8')).packages;
  const occurrences=Object.entries(lock).filter(([key])=>key.endsWith('node_modules/braces'));
  const consumers=Object.entries(lock).filter(([,row])=>row.dependencies?.braces||row.optionalDependencies?.braces);
  if(!occurrences.length||occurrences.some(([,row])=>row.dev!==true)||consumers.some(([key])=>fs.existsSync(path.join(app,key))))fail('required braces missing');
  console.log(JSON.stringify({status:'ABSENT_DEV_ONLY',libraryFixedUpstream:false}));
 } else {
  if(fs.realpathSync(root)!==root||fs.lstatSync(root).isSymbolicLink())fail('symlink package root refused');
  const packageBytes=fs.readFileSync(path.join(root,'package.json'));
  if(sha(packageBytes)!==manifest.packageJSONSHA256)fail('package/version preimage changed; review upstream update');
  if(sha(fs.readFileSync(path.join(root,'LICENSE')))!==manifest.upstreamLicenseSHA256)fail('license preimage changed');
  const writes=[];
  for(const name of expected){
   const item=manifest.files[name],file=path.join(root,name);
   if(sha(Buffer.from(item.contents))!==item.afterSHA256)fail('patch payload hash mismatch '+name);
   if(fs.realpathSync(path.dirname(file))!==path.dirname(file))fail('symlink parent refused '+name);
   if(fs.existsSync(file)&&fs.lstatSync(file).isSymbolicLink())fail('symlink file refused '+name);
   const current=fs.existsSync(file)?sha(fs.readFileSync(file)):null;
   if(current===item.afterSHA256)continue;
   if(check)fail('missing or incomplete patch '+name);
   if(current!==item.beforeSHA256)fail('unexpected installed source '+name);
   writes.push({file,contents:item.contents});
  }
  // A restarted install can resume only from exact known pre/postimages. Any
  // other file state was rejected above before any mutation. Install/build
  // must hold the project stack reservation; do not patch a live service.
  writes.sort((a,b)=>Number(!a.file.endsWith('/security-depth.js'))-Number(!b.file.endsWith('/security-depth.js')));
  for(const row of writes)fs.writeFileSync(row.file,row.contents);
  for(const name of expected)if(sha(fs.readFileSync(path.join(root,name)))!==manifest.files[name].afterSHA256)fail('postimage mismatch '+name);
  console.log(JSON.stringify({status:check?'VERIFIED':'APPLIED',filesWritten:writes.length,version:'3.0.3',libraryFixedUpstream:false,control:'downstream bounded depth/AST'}));
 }
} catch(error) {console.error(error.message);process.exitCode=1;}

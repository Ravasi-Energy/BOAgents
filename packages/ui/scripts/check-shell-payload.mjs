// Build regression: routes without Markdown must not download its renderer at startup.
import fs from 'node:fs';import path from 'node:path';import assert from 'node:assert/strict';
const root=path.resolve(process.argv[2]||'.next'),out=[];
for(const route of ['bo/routing','jobs','people']){
 const html=fs.readFileSync(path.join(root,'server/app',route+'.html'),'utf8');
 const scripts=[...new Set([...html.matchAll(/<script[^>]+src="(\/_next\/[^"?]+)/g)].map(m=>m[1]))];
 assert(scripts.length>0,'actual built scripts required');
 const bytes=scripts.reduce((sum,p)=>sum+fs.statSync(path.join(root,p.slice('/_next/'.length))).size,0);
 out.push({route,bytes,scripts:scripts.length,budget:800000});
}
console.log(JSON.stringify(out,null,2));assert(out.every(r=>r.bytes<r.budget),'initial route scripts must be below800KB, retain dynamic renderer for actual messages');

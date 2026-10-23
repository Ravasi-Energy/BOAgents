/* R01: upstream advisories published 22 October. Inert markup only, no evaluator or external network. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const ui=path.resolve(process.env.BO_OCT22_UI||fileURLToPath(new URL('..',import.meta.url)));
const require=createRequire(ui+'/package.json');
const lock=JSON.parse(fs.readFileSync(process.env.BO_OCT22_LOCK||ui+'/package-lock.json','utf8'));
const atLeast=(actual,minimum)=>{const a=actual.split('.').map(Number),b=minimum.split('.').map(Number);assert(a.length===3&&a.every(Number.isInteger),'Stable package version required');for(let i=0;i<3;i++){if(a[i]!==b[i])return a[i]>b[i];}return true;};
for(const name of ['next','@next/env','eslint-config-next','@next/eslint-plugin-next'])test('R01 Next October22 patched lock floor: '+name,()=>{assert(atLeast(lock.packages['node_modules/'+name].version,'16.4.1'));});
test('R01 every DOMPurify occurrence has the October22 patch and integrity',()=>{const rows=Object.entries(lock.packages).filter(([name])=>name.endsWith('node_modules/dompurify'));assert(rows.length);for(const [name,row]of rows){assert(atLeast(row.version,'3.4.17'),name);assert.match(row.integrity,/^sha512-/);}});

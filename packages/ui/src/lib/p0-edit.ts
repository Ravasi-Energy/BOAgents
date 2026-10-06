// Sparse edits against the version actually read. Removing a list entry always
// needs a separately named operation; absence/[] are never deletion intent.
export class P0EditError extends Error {}
export const CSV_SETTINGS = new Set(['bo.router.allowed_providers','bo.router.allowed_regions','bo.router.required_capabilities']);
type Doc = Record<string, unknown>;
const object = (v: unknown): v is Doc => v !== null && typeof v === 'object' && !Array.isArray(v);
const equal = (a: unknown,b: unknown) => JSON.stringify(a) === JSON.stringify(b);
export const csvItems = (value: string) => [...new Set(value.split(',').map(v=>v.trim()).filter(Boolean))];
export function sparseDelta(draft: Doc, base: Doc): Doc {
  const out: Doc = {};
  for (const [key,value] of Object.entries(draft)) {
    if (equal(value,base[key])) continue;
    if (object(value) && object(base[key])) {
      const delta=sparseDelta(value,base[key]);if(Object.keys(delta).length)out[key]=delta;
    } else out[key]=value;
  }
  return out;
}
export function trustDelta(draft: Doc, base: Doc): Doc {
  const out=sparseDelta(draft,base);
  for(const [key,id] of [['publishers','publisherId'],['keys','keyId']]) {
    if(!Array.isArray(draft[key]))continue;
    const previous=Array.isArray(base[key])?base[key] as Doc[]:[];
    const changes=(draft[key] as Doc[]).flatMap(row=>{
      const old=previous.find(p=>p[id]===row[id]);
      if(!old)return [row];
      const delta=sparseDelta(row,old);
      return Object.keys(delta).length?[{[id]:row[id],...delta}]:[];
    });
    delete out[key];if(changes.length)out[key]=changes;
  }
  // Explicit remove operations already present in the JSON editor are retained.
  return out;
}
export function settingPatch(key:string,draft:string,base:{value:unknown;version:number},removeText='') {
  if(!Number.isSafeInteger(base.version)||base.version<0)throw Error('Versiunea citită lipsește.');
  if(CSV_SETTINGS.has(key)) {
    const previous=csvItems(String(base.value??''));
    const add=csvItems(draft).filter(v=>!previous.includes(v));
    const remove=csvItems(removeText);
    return {value:add.join(','),expected_version:base.version,...(remove.length?{remove}:{})};
  }
  if(key==='bo.packages.trust_store_json') {
    const value=JSON.parse(draft),old=JSON.parse(String(base.value||'{}'));
    if(!object(value)||!object(old))throw Error('Document JSON obiect obligatoriu.');
    const delta=trustDelta(value,old);
    if(!Object.keys(delta).length)throw Error('Nicio modificare explicită de salvat.');
    return {value:JSON.stringify(delta),expected_version:base.version};
  }
  return {value:draft,expected_version:base.version};
}
export function profileDelta(draft:Doc,base:Doc) {
  const delta=sparseDelta(draft,base);
  for(const key of ['vendors','tickers'])if(Array.isArray(draft[key])) {
    const old=Array.isArray(base[key])?base[key] as unknown[]:[];
    const add=(draft[key] as unknown[]).filter(x=>!old.includes(x));
    delete delta[key];if(add.length)delta[key]=add;
  }
  return delta;
}

import type {BoCatalogEntry} from './bo';
export function catalogPatch(form:Doc,original:Doc,base:BoCatalogEntry,remove:{capabilities?:string;regions?:string}={}) {
  if(!Number.isSafeInteger(base.version)||base.version<1)throw Error('Versiunea citită lipsește.');
  const delta=sparseDelta(form,original);
  for(const key of ['capabilities','regions']) {
    if(Array.isArray(form[key])) {
      const old=(original[key]||[]) as unknown[];
      const add=(form[key] as unknown[]).filter(v=>!old.includes(v));
      delete delta[key];if(add.length)delta[key]=add;
    }
    const names=csvItems(remove[key as keyof typeof remove]||'');if(names.length)delta[key+'_remove']=names;
  }
  return {provider:base.provider,model_id:base.model_id,purpose:base.purpose,source:base.source,...delta,expected_version:base.version};
}

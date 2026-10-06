type Doc = Record<string, unknown>;
export type BotEdit = { name: string; description: string; content: Doc };
type BotDelta = { expected_version: number; name?: string; description?: string; content?: Doc; steps_remove?: string[]; capability_refs_remove?: string[]; policy_refs_remove?: string[] };
const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
function object(v: unknown): v is Doc { return !!v && typeof v === "object" && !Array.isArray(v); }
function sparse(base: Doc, draft: Doc, removals = new Set<string>()): Doc {
  const delta: Doc = {};
  for (const [k,v] of Object.entries(draft)) {
    if (equal(v,base[k])) continue;
    if (Array.isArray(v) && Array.isArray(base[k]) && !removals.has(k) &&
        (base[k] as unknown[]).some(old => !v.some(next => equal(old, next))))
      throw Error("Eliminarea listei " + k + " cere o operație explicită disponibilă în contract.");
    if (object(v) && object(base[k])) { const d=sparse(base[k],v); if(Object.keys(d).length)delta[k]=d; }
    else delta[k]=v;
  }
  return delta;
}
function steps(v: unknown): Doc[] {
  if (!Array.isArray(v) || v.some(e=>!object(e)||typeof e.id!=="string"||!e.id.trim())) throw Error("Pași cu ID stabil necesari.");
  if (new Set(v.map(e=>e.id)).size!==v.length) throw Error("ID pas duplicat.");
  return v as Doc[];
}
export function buildBotDelta(base: BotEdit, draft: BotEdit, version: number): BotDelta {
  if (!Number.isInteger(version)||version<1||!object(draft.content))throw Error("Bază/versiune invalidă.");
  const payload: BotDelta={expected_version:version};
  if (draft.name!==base.name)payload.name=draft.name;
  if (draft.description!==base.description)payload.description=draft.description;
  const content=sparse(base.content,draft.content,new Set(["steps","capability_refs","policy_refs"]));
  if ('steps' in draft.content) {
    const prior=steps(base.content.steps??[]),next=steps(draft.content.steps);
    const changed=next.flatMap(row=>{const old=prior.find(e=>e.id===row.id);if(!old)return[row];const d=sparse(old,row);return Object.keys(d).length?[{id:row.id,...d}]:[];});
    delete content.steps;
    if(changed.length)content.steps=changed;
    const remove=prior.filter(e=>!next.some(n=>n.id===e.id)).map(e=>e.id as string);
    if(remove.length)payload.steps_remove=remove;
  }
  for(const field of ['capability_refs','policy_refs'] as const){
    if(!(field in draft.content))continue;
    const before=base.content[field]??[],after=draft.content[field];
    if(!Array.isArray(before)||!Array.isArray(after)||after.some(e=>typeof e!=='string'))throw Error("Listă invalidă: "+field);
    delete content[field];
    const add=after.filter(e=>!before.includes(e));if(add.length)content[field]=add;
    const remove=before.filter(e=>!after.includes(e));
    if(remove.length)payload[`${field}_remove`]=remove as string[];
  }
  if(Object.keys(content).length)payload.content=content;
  return payload;
}
function merge(base: Doc, delta: Doc): Doc {
  const out={...base};for(const[k,v]of Object.entries(delta))out[k]=object(v)&&object(out[k])?merge(out[k],v):v;return out;
}
export function rebaseBotDraft(base: BotEdit, draft: BotEdit, fresh: BotEdit): BotEdit {
  const delta=buildBotDelta(base,draft,1),content=merge(fresh.content,delta.content??{});
  if (delta.content?.steps) {
    const current=steps(fresh.content.steps??[]).map(e=>({...e}));
    for(const row of steps(delta.content.steps)){
      const at=current.findIndex(e=>e.id===row.id),old=steps(base.content.steps??[]).find(e=>e.id===row.id);
      if(old&&at<0)throw Error("Pasul editat a fost eliminat; draftul rămâne local.");
      if(!old&&at>=0)throw Error("ID nou ocupat; draftul rămâne local.");
      if(at<0)current.push(row);else current[at]=merge(current[at],row);
    }
    content.steps=current;
  }
  if(delta.steps_remove)content.steps=steps(content.steps??[]).filter(e=>!delta.steps_remove!.includes(e.id as string));
  for(const field of ['capability_refs','policy_refs'] as const){
    const add=delta.content?.[field]??[],remove=delta[`${field}_remove`]??[];
    content[field]=[...new Set([...(fresh.content[field] as string[]??[]),...(add as string[])])].filter(e=>!remove.includes(e));
  }
  return{name:delta.name??fresh.name,description:delta.description??fresh.description,content};
}

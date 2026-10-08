"use client";
import { useEffect, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import { money } from "./money-format";
import { getBoSettings, putBoSetting } from "./bo";
import { MONEY_KEY, serverMoney, type ServerMoney } from "./money-server";
import { MONEY_SERVER_EVENT, moneyEpoch } from "./money-event.ts";
const EVENT = MONEY_SERVER_EVENT;
// Coalesce concurrent readers only. No cached browser preference or cross-user snapshot.
const pending = new Map<string, Promise<ServerMoney>>();
function read(identity: string): Promise<ServerMoney> {
  const key=identity+":"+moneyEpoch();const existing=pending.get(key); if(existing)return existing;
  const request=getBoSettings().then(serverMoney).finally(()=>{if(pending.get(key)===request)pending.delete(key);});
  pending.set(key,request);return request;
}
function useServerMoney() {
  const {data:session}=useSession();const identity=session?.user?.email ?? "";
  const [state,setState]=useState<{identity:string;row:ServerMoney|null;error:string}>({identity:"",row:null,error:"Se citește setarea server pentru bani."});
  useEffect(()=>{
    let active=true,sequence=0;
    const refresh=async()=>{const current=++sequence;setState({identity,row:null,error:identity?"Se citește setarea server pentru bani.":"Autentificarea este necesară pentru citirea setării banilor."});if(!identity)return;
      try {const row=await read(identity);if(active&&current===sequence)setState({identity,row,error:""});}
      catch(error){if(active&&current===sequence)setState({identity,row:null,error:error instanceof Error?"Setarea server nu a putut fi citită: "+error.message:"Setarea server nu a putut fi citită."});}};
    void refresh();window.addEventListener("focus",refresh);window.addEventListener(EVENT,refresh);
    return()=>{active=false;window.removeEventListener("focus",refresh);window.removeEventListener(EVENT,refresh);};
  },[identity]);
  return {identity,row:state.identity===identity?state.row:null,error:state.identity===identity?state.error:"Se verifică identitatea pentru citirea setării banilor."};
}
export function Money({ amount, currency }: { amount: unknown; currency: unknown }) {
  const {row,error}=useServerMoney();const raw=typeof amount==="string"||typeof amount==="number"?String(amount):"necunoscută";
  if(!row)return <span role="status">{raw} — {error}</span>;
  const rendered=money(amount,currency,row.value);const rounded=!rendered.includes(" — ")&&row.value===0&&/^-?\d+\.\d*[1-9]\d*$/.test(raw);
  return <span title={rounded?`Valoare exactă: ${raw} ${String(currency)}`:undefined}>{rounded?"≈ ":""}{rendered}</span>;
}
export function UnconfiguredMoney({amount}:{amount:number|string}) {return <Money amount={amount} currency={null}/>;}
export function MoneySettings() {
  const config=useServerMoney();const [base,setBase]=useState<{identity:string;row:ServerMoney}|null>(null);const [draft,setDraft]=useState("2"),[message,setMessage]=useState(""),[busy,setBusy]=useState(false);
  const identity=useRef(config.identity);if(identity.current!==config.identity){identity.current=config.identity;}
  useEffect(()=>{setBase(null);setDraft("2");setMessage("");},[config.identity]);
  useEffect(()=>{if(config.row && !base){setBase({identity:config.identity,row:config.row});setDraft(String(config.row.value));}},[config.row,config.identity,base]);
  async function save(){if(!base||base.identity!==config.identity||config.row?.role!=="admin")return;setBusy(true);setMessage("");try{const result=await putBoSetting(MONEY_KEY,Number(draft),base.row.version);setBase({identity:config.identity,row:{...base.row,value:result.setting.value as 0|2,version:result.setting.version}});setMessage("Salvat pe server. Doar afișarea se schimbă; sumele stocate rămân exacte.");}catch(error){setMessage(error instanceof Error?error.message:"Salvarea a fost refuzată.");}finally{setBusy(false);}}
  async function rebase(){if(!config.identity)return;setBusy(true);try{const row=await read(config.identity);setBase({identity:config.identity,row});setMessage("Versiunea curentă a fost citită. Draftul este păstrat; verifică și salvează explicit.");}catch(error){setMessage(error instanceof Error?error.message:"Citirea a fost refuzată.");}finally{setBusy(false);}}
  return <section aria-label="Afișarea sumelor"><h2>Afișarea sumelor</h2>
    <label>Zecimale <select value={draft} disabled={busy||!config.row||config.row.role!=="admin"} onChange={event=>setDraft(event.target.value)}><option value="0">0 — 6.000 lei</option><option value="2">2 — 6.000,00 lei</option></select></label>
    <button type="button" className="bo-btn" disabled={busy||!base||base.identity!==config.identity||config.row?.role!=="admin"} onClick={()=>void save()}>Salvează</button>
    <button type="button" className="bo-btn" disabled={busy||!config.identity} onClick={()=>void rebase()}>Reîncarcă și păstrează editura</button>
    <p>Setare de server pentru firmă. Valuta provine din date; nu se face conversie valutară.</p>
    {config.row&&config.row.role!=="admin"&&<p>Doar citire — modificarea cere rol administrator.</p>}
    {(config.error||message)&&<p role="status">{message||config.error}</p>}
  </section>;
}

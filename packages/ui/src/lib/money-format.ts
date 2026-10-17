/// <reference lib="es2022.intl" />
// One shared contract/source copied byte-for-byte to each product's existing money module.
// Accepted codes remain the existing runtime ISO registry. Historical validity/owner registry
// is a backend contract dependency: never invent a missing currency or migrate stored money.
import {ISO_CURRENCIES} from "./money-currencies.ts";
export const MONEY_PRESENTATION_VERSION = "stefan-money-2.0-20261017";
const MAX_CANONICAL_DIGITS = 1024; // presentation safety bound, NOT an operation precision policy
function decimal(amount: unknown): {negative:boolean; integer:string; fraction:string} {
  // A legacy safe integer is exact. Fractional JSON numbers cannot prove the original decimal.
  if (typeof amount === "number" && !Number.isSafeInteger(amount)) throw new Error("Sumă numerică fără precizie sigură și exactă; este necesar șirul zecimal original.");
  if (typeof amount !== "string" && typeof amount !== "number") throw new Error("Sumă invalidă; lipsa nu este zero.");
  const raw=String(amount);if(raw.length>MAX_CANONICAL_DIGITS+2)throw new Error("Suma depășește limita de prezentare.");
  const m=/^(-?)(0|[1-9]\d*)(?:\.(\d+))?$/.exec(raw);if(!m)throw new Error("Sumă invalidă: este necesar un șir zecimal canonic în unități principale.");
  return {negative:!!m[1],integer:m[2],fraction:m[3]??""};
}
function currencyLabel(currency:unknown):string {
  if(typeof currency!=="string"||(!ISO_CURRENCIES.has(currency)||currency==="XXX"))throw new Error("Valută necunoscută sau invalidă; operațiunea monetară este blocată.");
  return currency==="RON"?"lei":currency==="EUR"?"€":currency;
}
function grouped(integer:string):string{return integer.replace(/\B(?=(\d{3})+(?!\d))/g,".");}
/** Exact major units. Never scale /100; half-up presentation only, both signs. */
export function formatMoney(amount:unknown,currency:unknown,decimals:number):string {
  if(decimals!==0&&decimals!==2)throw new Error("Zecimale: doar 0 sau 2, din Setări.");
  const label=currencyLabel(currency),v=decimal(amount),scale=BigInt(10)**BigInt(decimals);
  const exactFraction=v.fraction.replace(/0+$/,"");
  const rounded=v.fraction.length>decimals&&/[1-9]/.test(v.fraction.slice(decimals));
  let units=BigInt(v.integer)*scale+BigInt((v.fraction+"00").slice(0,decimals)||"0");
  if((v.fraction[decimals]??"0")>="5")units+=BigInt(1);
  const short=(v.negative&&units!==BigInt(0)?"-":"")+grouped((units/scale).toString())+(decimals?","+(units%scale).toString().padStart(2,"0"):"")+" "+label;
  if(!rounded)return short;
  const exact=(v.negative&&(BigInt(v.integer)!==BigInt(0)||!!exactFraction)?"-":"")+grouped(v.integer)+(exactFraction?","+exactFraction:"")+" "+label;
  return "≈ "+short+" — Valoare exactă: "+exact;
}
export type MoneyInputPolicy={maxScale:number;maxIntegerDigits:number};
// Existing input operations retain their explicit two-place contract until owners publish
// per-operation schema. This default is not applied by the formatter to arbitrary currencies.
export const LEGACY_MONEY_INPUT_POLICY:MoneyInputPolicy={maxScale:2,maxIntegerDigits:30};
export function parseMoneyInput(input:string,policy:MoneyInputPolicy=LEGACY_MONEY_INPUT_POLICY):string|null {
  if(!Number.isSafeInteger(policy.maxScale)||policy.maxScale<0||!Number.isSafeInteger(policy.maxIntegerDigits)||policy.maxIntegerDigits<1||policy.maxScale+policy.maxIntegerDigits>MAX_CANONICAL_DIGITS)throw new Error("Contract de precizie monetară invalid.");
  const raw=input.trim();if(!raw)return null;
  if(raw.length>MAX_CANONICAL_DIGITS+Math.ceil(MAX_CANONICAL_DIGITS/3)+2||! /^-?(?:\d+|[1-9]\d{0,2}(?:\.\d{3})+)(?:,\d+)?$/.test(raw))throw new Error("Sumă invalidă. Folosește formatul românesc 6.000,25, fără simbol de valută.");
  const canonical=raw.replace(/\./g,"").replace(",",".");const [integer,fraction]=canonical.split(".");const negative=integer.startsWith("-");const digits=integer.replace("-","").replace(/^0+(?=\d)/,"");
  if(digits.length>policy.maxIntegerDigits||(fraction?.length??0)>policy.maxScale)throw new Error(`Suma depășește contractul operațiunii: ${policy.maxIntegerDigits} cifre întregi și ${policy.maxScale} zecimale. Nu a fost salvată.`);
  const nonzero=/[1-9]/.test(digits+(fraction??""));return(negative&&nonzero?"-":"")+digits+(fraction===undefined?"":"."+fraction);
}
/** Edit exactly the stored decimal, never a rounded display string. */
export function moneyInputValue(amount:unknown):string {
  if(amount===null||amount===undefined||amount==="")return "";
  if(typeof amount==="number"&&!Number.isSafeInteger(amount))return String(amount)+" — valoare zecimală legacy fără precizie exactă; clarifică sursa.";
  const v=decimal(amount);return(v.negative&&/[1-9]/.test(v.integer+v.fraction)?"-":"")+grouped(v.integer)+(v.fraction?","+v.fraction:"");
}
/** Deprecated JSON-number writes: only exact safe integers, never fractional intermediates. */
export function parseMoneyNumber(input:string):number|null {
  const raw=parseMoneyInput(input);if(raw===null)return null;
  if(raw.includes(".")&&!/\.0+$/.test(raw))throw new Error("Câmpul numeric legacy nu poate păstra contractul zecimal exact; este necesar API cu șir zecimal.");
  const integer=BigInt(raw.split(".")[0]);if(integer>BigInt(Number.MAX_SAFE_INTEGER)||integer<BigInt(Number.MIN_SAFE_INTEGER))throw new Error("Suma depășește limita numerică exactă.");return Number(integer);
}
export function moneyCurrency(input:string):string|null {
  const value=input.trim();if(!value)return null;currencyLabel(value);return value;
}
export function money(amount:unknown,currency:unknown,decimals:number):string {
  // Invalid/missing values are visible diagnostics, never implicit zero or lei.
  const original=typeof amount==="string"||typeof amount==="number"?String(amount):"sumă absentă";
  try{return formatMoney(amount,currency,decimals);}catch(e){return original+" — "+(e instanceof Error?e.message:"Sumă invalidă");}
}
/** Published execution _validate_budget: (0,1000000], exact Decimal; UI safety cap1000 fractional places. */
export function executionBudgetValue(input:string):string {
 const value=parseMoneyInput(input,{maxScale:1000,maxIntegerDigits:7});
 if(value===null)throw new Error("Completează suma bugetului și valuta.");
 const [integer,fraction=""]=value.split(".");
 if(value.startsWith("-")||!/[1-9]/.test(value)||BigInt(integer)>BigInt(1000000)||(BigInt(integer)===BigInt(1000000)&&/[1-9]/.test(fraction)))throw new Error("Bugetul trebuie să fie mai mare ca zero și cel mult 1.000.000, fără rotunjire.");
 return value;
}
export function requiredMoneyInput(input:string,currency:unknown=null):string {currencyLabel(currency);return executionBudgetValue(input);}
/** AC5: execution budgets are monetary; legacy NULL is diagnostic, never an execution unit. */
export function requiredExecutionBudgetInput(input:string,currency:unknown,abstractUnits:boolean):string {
 if(abstractUnits)throw new Error("Bugetul de execuție este monetar; completează valuta confirmată.");
 return requiredMoneyInput(input,currency);
}

export function retainerMoneyPatch(amount: string, currency: string, existingStructured = false) {
  const value = parseMoneyInput(amount);
  const code = moneyCurrency(currency);
  if ((value === null) !== (code === null)) throw new Error("Completează atât suma, cât și valuta onorariului, sau golește ambele câmpuri.");
  if (value === null && existingStructured) throw new Error("Ștergerea onorariului structurat nu este disponibilă prin API. Datele existente au fost păstrate.");
  if (value !== null && (value.length > 32 || !/^\d+(?:\.\d{1,2})?$/.test(value))) throw new Error("Onorariul cere o sumă pozitivă sau zero, de maximum 32 caractere și 2 zecimale. Nu a fost salvat.");
  return { retainer_amount: value, retainer_currency: code };
}


/** routing/catalog.py _MONEY_RE: six fractional places, nonnegative, unknown price stays null. */
export function parseRoutingPrice(input:string):string|null {
  const value=parseMoneyInput(input,{maxScale:6,maxIntegerDigits:30});
  if(value?.startsWith("-"))throw new Error("Prețul de catalog nu poate fi negativ.");
  return value;
}

/** Validate the amount/currency pair only when a monetary operation is changed, not an unrelated delta. */
export function validateMoneyPair(amount:unknown,currency:string|null):void {
 if(amount===null||amount===undefined){if(currency!==null)throw new Error("Completează suma și valuta împreună.");return;}
 currencyLabel(currency);decimal(amount);
}

/** Published profile stores Decimal directly; do not impose the invoice/retainer two-place rule. */
export function parseProfileMoney(input:string):string|null {return parseMoneyInput(input,{maxScale:512,maxIntegerDigits:512});}

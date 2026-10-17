import {formatMoney}from"./money-format.ts";
/** Published audit.py UsageTotals.cost: exact major decimal+ISO; cost_usd is never a fallback. */
export function usageCost(row:unknown):{amount:string;currency:string}{
 if(!row||typeof row!=="object")throw new Error("Costul exact lipsește.");
 const cost=(row as {cost?:unknown}).cost;
 if(!cost||typeof cost!=="object")throw new Error("Costul exact și valuta lipsesc; valoarea numerică legacy nu este utilizată.");
 const {amount,currency}=cost as {amount?:unknown;currency?:unknown};
 if(typeof amount!=="string"||typeof currency!=="string")throw new Error("Costul cere valoare zecimală exactă și valută explicită.");
 formatMoney(amount,currency,2);return{amount,currency};
}

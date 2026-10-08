export const MONEY_SERVER_EVENT = 'bo-money-server-change';
let epoch=0;
export function moneyEpoch():number{return epoch;}
/** A successful catalog/context write invalidates in-flight readers from the previous context. */
export function moneyChanged():void{
  epoch++;
  if(typeof window!=='undefined')window.dispatchEvent(new Event(MONEY_SERVER_EVENT));
}

/** ff2a3a3 published catalog contract, existing /bo/settings wire shape. */
export const MONEY_KEY = 'bo.ui.money_display_decimals';
export type ServerMoney = { value: 0 | 2; version: number; role: 'admin' | 'operator' | 'viewer'; tenant: string };
export function serverMoney(data: unknown): ServerMoney {
  if (!data || typeof data !== 'object') throw new Error('Catalogul banilor nu a putut fi citit.');
  const d=data as {settings?:unknown;role?:unknown;tenant?:unknown};
  const row=Array.isArray(d.settings)?d.settings.find(x=>x && x.key===MONEY_KEY):null;
  if (!row || (row.value!==0 && row.value!==2) || !Number.isSafeInteger(row.version) || row.version<0 || !['admin','operator','viewer'].includes(String(d.role)) || typeof d.tenant!=='string') throw new Error('Setarea server pentru bani lipsește sau este invalidă; nu se presupun 2 zecimale.');
  return {value:row.value,version:row.version,role:d.role as ServerMoney['role'],tenant:d.tenant};
}

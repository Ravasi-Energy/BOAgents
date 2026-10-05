import type { Person, PersonPatch } from './api';
export function personEdit(person: Person) {
  return { full_name: person.full_name, role: person.role, email: person.email ?? '',
    slack_user_id: person.slack_user_id ?? '', telegram_chat_id: person.telegram_chat_id ?? '',
    discord_user_id: person.discord_user_id ?? '', preferred_channel: person.preferred_channel,
    response_sla_hours: String(person.response_sla_hours), on_leave_until: person.on_leave_until ?? '',
    authority_scope: [...person.authority_scope], availability: person.availability.map(w => ({...w,weekdays:[...w.weekdays]})) };
}
export type PersonEdit = ReturnType<typeof personEdit>;
function changed(form: PersonEdit, base: PersonEdit): Partial<PersonEdit> {
  const delta: Partial<PersonEdit> = {};
  for (const key of Object.keys(base) as (keyof PersonEdit)[]) {
    if (JSON.stringify(form[key]) !== JSON.stringify(base[key])) Object.assign(delta, {[key]: form[key]});
  }
  return delta;
}
export function rebasePersonEdit(form: PersonEdit, base: Person, fresh: Person): PersonEdit {
  return {...personEdit(fresh), ...changed(form, personEdit(base))};
}
export function personPatch(form: PersonEdit, base: Person): PersonPatch {
  if (!Number.isSafeInteger(base.version) || base.version < 1) throw new Error('Versiunea citită lipsește; reîncarcă persoana.');
  const delta = changed(form, personEdit(base));
  if (!Object.keys(delta).length) throw new Error('Nicio modificare de salvat.');
  const patch: PersonPatch = {expected_version: base.version};
  for (const key of Object.keys(delta) as (keyof PersonEdit)[]) {
    const value = delta[key];
    if (key === 'response_sla_hours') {
      const hours = Number(value); if (!Number.isInteger(hours) || hours < 1) throw new Error('SLA must be a positive integer.');
      patch.response_sla_hours = hours;
    } else if (['email','slack_user_id','telegram_chat_id','discord_user_id'].includes(key) && typeof value === 'string' && !value.trim()) {
      throw new Error('Ștergerea acestui contact nu este suportată de contractul serverului; draftul este păstrat.');
    } else if (key === 'on_leave_until' && !value) patch.clear_on_leave = true;
    else if (typeof value === 'string') Object.assign(patch, {[key]: ['full_name','role'].includes(key) ? value.trim() : value.trim() || null});
    else Object.assign(patch, {[key]: value});
  }
  return patch;
}

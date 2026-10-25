// Mesajul unei acțiuni pilot refuzate. Serverul întoarce 409 atât pentru
// conflictul CAS, cât și pentru refuzurile de politică/configurație (allowlist,
// supervizare Guardian, reconciliere cerută) — și pe ambele cu codul
// version_conflict. Textul serverului spune deja când reîncărcarea ajută
// („reîncarcă înainte de salvare”), deci UI-ul nu adaugă o instrucțiune
// generică de reîncărcare care ar fi falsă pentru un refuz de politică.
export function pilotActionNotice(status: number, code: string, detail: unknown): string {
  const text = typeof detail === "string" && detail ? detail : code;
  return (status === 409 ? "Refuz de stare (409): " : "Acțiune refuzată: ") + text;
}

export const PILOT_UNCONFIRMED_TEXT =
  "Cererea nu a fost confirmată; verifică dovezile înainte de repetare.";

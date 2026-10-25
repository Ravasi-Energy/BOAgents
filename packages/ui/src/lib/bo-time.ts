// Timestamps on BO pages follow the tenant zone from bo.ui.timezone (its
// declared effect: "Se aplică imediat la timestampurile afișate în paginile
// BO"), never the browser's zone. Without a readable zone the original UTC is
// shown and labelled as such.
// dateOnly gives the tenant's calendar day (a UTC slice is the wrong day near midnight).
export function formatBoTime(iso: string, timeZone: string | null, timeOnly = false, dateOnly = false): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const options: Intl.DateTimeFormatOptions = dateOnly
    ? { year: "numeric", month: "2-digit", day: "2-digit" }
    : timeOnly
    ? { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }
    : { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" };
  if (timeZone) {
    try {
      return new Intl.DateTimeFormat("ro-RO", { ...options, timeZone }).format(date);
    } catch {
      /* invalid zone: fall through to labelled UTC */
    }
  }
  return `${new Intl.DateTimeFormat("ro-RO", { ...options, timeZone: "UTC" }).format(date)} UTC`;
}

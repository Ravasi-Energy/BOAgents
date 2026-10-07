/** Server timestamps are instants. Display is in the catalog timezone, never the browser zone. */
export function formatRunAt(iso: string, timeZone: string | null = "Europe/Bucharest", now = Date.now()): { absolute: string; relative: string } {
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(iso)) return { absolute: `${iso} — fus orar absent`, relative: "" };
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return { absolute: `${iso} — dată invalidă`, relative: "" };
  let absolute = `${iso} — fus orar necitit`;
  if (timeZone) {
    try {
      absolute = `${new Intl.DateTimeFormat("ro-RO", { timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).format(date)} (${timeZone})`;
    } catch { absolute = `${iso} — fus orar invalid: ${timeZone}`; }
  }
  const delta = date.getTime() - now, abs = Math.abs(delta);
  const mins = Math.round(abs / 60_000), hours = Math.round(abs / 3_600_000), days = Math.round(abs / 86_400_000);
  const unit = mins < 60 ? `${mins}m` : hours < 48 ? `${hours}h` : `${days}d`;
  return { absolute, relative: delta >= 0 ? `in ${unit}` : `${unit} ago` };
}

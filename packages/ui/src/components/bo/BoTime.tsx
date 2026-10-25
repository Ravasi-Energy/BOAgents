"use client";
import { useCalendarTimezone } from "@/lib/CalendarTimezone";
import { formatBoTime } from "@/lib/bo-time";

export default function BoTime({ iso, timeOnly = false, dateOnly = false }: { iso: string; timeOnly?: boolean; dateOnly?: boolean }) {
  const timeZone = useCalendarTimezone();
  return <time dateTime={iso}>{formatBoTime(iso, timeZone, timeOnly, dateOnly)}</time>;
}

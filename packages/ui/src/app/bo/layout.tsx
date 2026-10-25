import { CalendarTimezoneProvider } from "@/lib/CalendarTimezone";

// Every BO page reads the tenant zone once, so timestamps follow bo.ui.timezone.
export default function BoLayout({ children }: { children: React.ReactNode }) {
  return <CalendarTimezoneProvider>{children}</CalendarTimezoneProvider>;
}

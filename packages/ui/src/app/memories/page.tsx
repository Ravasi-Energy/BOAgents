"use client";

import { CalendarTimezoneProvider } from "@/lib/CalendarTimezone";
import PulsePage from "@/components/memories/PulsePage";

export default function MemoriesPage() {
  return (
    <main className="flex-1 min-h-0 overflow-y-auto">
      <CalendarTimezoneProvider><PulsePage /></CalendarTimezoneProvider>
    </main>
  );
}

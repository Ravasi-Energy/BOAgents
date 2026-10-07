"use client";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { getBoSettings } from "./bo";
const CalendarTimezone = createContext<string | null>(null);
export function CalendarTimezoneProvider({ children }: { children: ReactNode }) {
  const [zone, setZone] = useState<string | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true, request = 0;
    const refresh = async () => {
      const current = ++request;
      try {
        const data = await getBoSettings();
        const value = data.settings.find(setting => setting.key === "bo.ui.timezone")?.value;
        if (typeof value !== "string" || !value) throw new Error("Fusul orar al serverului lipsește.");
        new Intl.DateTimeFormat("ro-RO", { timeZone: value });
        if (active && current === request) { setZone(value); setError(""); }
      } catch { if (active && current === request) { setZone(null); setError("Fusul orar nu a putut fi citit de pe server. Ora UTC originală este păstrată."); } }
    };
    void refresh();
    window.addEventListener("focus", refresh);
    return () => { active = false; window.removeEventListener("focus", refresh); };
  }, []);
  return <CalendarTimezone.Provider value={zone}>{error && <p role="alert">{error}</p>}{children}</CalendarTimezone.Provider>;
}
export function useCalendarTimezone() { return useContext(CalendarTimezone); }

import { useEffect, useMemo, useState } from "react";

/** What remains until `until`, in words: "2 d 4 h", "3 h 12 min", "under a minute", "passed". */
export function remaining(until: string, now: Date): string {
  const ms = new Date(until).getTime() - now.getTime();
  if (ms <= 0) return "passed";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return "under a minute";
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  if (days > 0) return `${days} d ${hours} h`;
  if (hours > 0) return `${hours} h ${mins} min`;
  return `${mins} min`;
}

/** The time now, ticking every second, offset to the server's clock when its `now` is known. */
export function useNow(serverNow?: string): Date {
  const offset = useMemo(
    () => (serverNow ? new Date(serverNow).getTime() - Date.now() : 0),
    [serverNow],
  );
  const [now, setNow] = useState(() => new Date(Date.now() + offset));
  useEffect(() => {
    setNow(new Date(Date.now() + offset));
    const timer = window.setInterval(() => setNow(new Date(Date.now() + offset)), 1000);
    return () => window.clearInterval(timer);
  }, [offset]);
  return now;
}

/** A date and time in the reader's own locale and zone. */
export function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function isPast(iso: string, now: Date): boolean {
  return new Date(iso).getTime() <= now.getTime();
}

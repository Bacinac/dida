// Every date and time the UI shows goes through here: one shape per kind of moment,
// in the active language, 24-hour in both. `toLocale*String` / `Intl.DateTimeFormat`
// anywhere else is a test failure (dt.test.ts).
import { i18n, type Locale } from "$lib/kit";

type When = Date | number | string;

const LOCALE: Record<Locale, string> = { hr: "hr-HR", en: "en-GB" };
const HM = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" } as const;
const HMS = { ...HM, second: "2-digit" } as const;

const cache = new Map<string, Intl.DateTimeFormat>();
function format(when: When, opts: Intl.DateTimeFormatOptions): string {
  const d = when instanceof Date ? when : new Date(when);
  if (isNaN(d.getTime())) return "";
  const key = i18n.locale + JSON.stringify(opts);
  let f = cache.get(key);
  if (!f) cache.set(key, (f = new Intl.DateTimeFormat(LOCALE[i18n.locale], opts)));
  return f.format(d);
}

/** 14:05 (14:05:07) */
export const clock = (d: When, seconds = false): string => format(d, seconds ? HMS : HM);

/** 23. 9. */
export const shortDate = (d: When): string => format(d, { day: "numeric", month: "numeric" });

/** uto, 23. 9. */
export const dayDate = (d: When): string => format(d, { weekday: "short", day: "numeric", month: "numeric" });

/** 23. 9. 14:05 — a recent moment that may not be today. */
export const shortDateTime = (d: When): string => format(d, { day: "numeric", month: "numeric", ...HM });

/** uto, 23. 9. 14:05 — an instant days back, where the clock alone doesn't say which evening. */
export const dayTime = (d: When): string =>
  format(d, { weekday: "short", day: "numeric", month: "numeric", ...HM });

/** 23. 9. 2026. 14:05 (14:05:07) — a record: a log line, a backup, a last login. */
export const dateTime = (d: When, seconds = false): string =>
  format(d, { day: "numeric", month: "numeric", year: "numeric", ...(seconds ? HMS : HM) });

/** 23. rujna 2026. */
export const longDate = (d: When): string => format(d, { day: "numeric", month: "long", year: "numeric" });

/** utorak, 23. rujna */
export const longDay = (d: When): string => format(d, { weekday: "long", day: "numeric", month: "long" });

/** 2026-09-23 in the browser's zone — a day key, never shown. */
export function isoDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Numbers and dates are shaped in one place each (formatNumber, $lib/dt): a
// hand-rolled `toFixed` shows 2.5 to a Croatian reader, and every inline
// `toLocaleString` picked its own locale, order and clock.
import { afterEach, describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { clock, dateTime, dayTime, isoDate, longDate, shortDateTime } from "$lib/dt";
import { formatNumber, i18n } from "$lib/kit";

const HOME = { "src/lib/dt.ts": true, "src/lib/kit/i18n.svelte.ts": true } as Record<string, boolean>;
const RAW = /\.toFixed\(|\.toLocale(?:Date|Time)?String\(|Intl\.(?:DateTimeFormat|NumberFormat)\(/;

function walk(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    if (statSync(p).isDirectory()) walk(p, out);
    else if ((p.endsWith(".svelte") || p.endsWith(".ts")) && !p.endsWith(".test.ts")) out.push(p);
  }
  return out;
}

describe("one place for numbers and dates", () => {
  afterEach(() => { i18n.locale = "hr"; });

  it("nothing formats a number or a date by hand", () => {
    const offenders = walk("src").filter((f) => !HOME[f]).flatMap((f) =>
      readFileSync(f, "utf8").split("\n").flatMap((line, i) => (RAW.test(line) ? [`${f}:${i + 1}`] : [])));
    expect(offenders).toEqual([]);
  });

  it("speaks Croatian by default and English on request, 24-hour in both", () => {
    const at = new Date(2026, 8, 23, 14, 5, 7);
    i18n.locale = "hr";
    expect(clock(at)).toBe("14:05");
    expect(clock(at, true)).toBe("14:05:07");
    expect(dateTime(at)).toBe("23. 09. 2026. 14:05");
    expect(longDate(at)).toBe("23. rujna 2026.");
    expect(formatNumber(1234.56, { maximumFractionDigits: 1 })).toBe("1.234,6");
    i18n.locale = "en";
    expect(clock(new Date(2026, 8, 23, 21, 40))).toBe("21:40");
    expect(shortDateTime(at)).toBe("23/09, 14:05");
    expect(formatNumber(1234.56, { maximumFractionDigits: 1 })).toBe("1,234.6");
  });

  it("an unreadable instant renders as nothing, not 'Invalid Date'", () => {
    expect(dayTime("not a date")).toBe("");
    expect(isoDate(new Date(2026, 0, 5))).toBe("2026-01-05");
  });
});

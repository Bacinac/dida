// A Tailwind class naming a colour token that doesn't exist doesn't throw and
// doesn't warn — the element just renders in the inherited colour. So a typo like
// `text-dida-muted` (the token is `dida-text-muted`) is invisible until someone
// notices the wrong shade. It has now happened twice in this codebase; this makes
// it a test failure instead.
import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

function walk(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (p.endsWith(".svelte") || p.endsWith(".ts")) out.push(p);
  }
  return out;
}

describe("colour tokens", () => {
  it("every dida-* colour class used in the app is actually defined", () => {
    const css = readFileSync("src/app.css", "utf8");
    const defined = new Set([...css.matchAll(/--color-(dida-[a-z0-9-]+)/g)].map((m) => m[1]));
    expect(defined.size).toBeGreaterThan(4); // sanity: we really did read the palette

    const offenders: string[] = [];
    for (const file of walk("src")) {
      if (file.endsWith(".test.ts")) continue;
      const src = readFileSync(file, "utf8");
      for (const m of src.matchAll(/(?:text|bg|border|ring|from|to)-(dida-[a-z0-9-]+)/g)) {
        // `dida-panel/60`-style opacity suffixes are stripped by the regex already.
        if (!defined.has(m[1])) offenders.push(`${file}: ${m[0]}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("text on the accent is readable (WCAG AA, 4.5:1)", () => {
    const css = readFileSync("src/app.css", "utf8");
    const tokens = new Map<string, string>();
    for (const [, name, value] of css.matchAll(/--([\w-]+):\s*([^;]+);/g)) if (!tokens.has(name)) tokens.set(name, value.trim());
    const hex = (name: string): string | undefined => {
      const value = tokens.get(name);
      const ref = value?.match(/^var\(--([\w-]+)\)$/);
      return ref ? hex(ref[1]) : value?.match(/^#[0-9a-f]{6}$/i)?.[0];
    };
    const lum = (h: string) => {
      const [r, g, b] = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
        .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const [fill, ink] = [hex("color-dida-accent-strong"), hex("color-dida-on-accent")];
    expect(fill && ink).toBeTruthy();
    const [hi, lo] = [lum(fill!), lum(ink!)].sort((a, b) => b - a);
    expect((hi + 0.05) / (lo + 0.05)).toBeGreaterThanOrEqual(4.5);
  });

  it("nothing puts text on the plain accent, which is 2.96:1 against white", () => {
    const offenders: string[] = [];
    for (const file of walk("src")) {
      if (file.endsWith(".test.ts")) continue;
      const src = readFileSync(file, "utf8");
      for (const m of src.matchAll(/["'`][^"'`]*\bbg-dida-accent(?![-/\w])[^"'`]*["'`]/g)) {
        if (/\btext-(white|black|dida-bg|dida-on-accent)\b/.test(m[0])) offenders.push(`${file}: ${m[0]}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

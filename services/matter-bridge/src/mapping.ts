// Pure DIDA <-> Matter conversions, split out of index.ts so they can be tested.
//
// index.ts opens Postgres, NATS and the Matter server at module load, so nothing
// there is importable from a test. These functions carry the part that is easy to
// get subtly wrong and expensive when it is: a bad level or system-mode mapping
// mis-actuates a real device through Google/Alexa, with no DIDA-side symptom.

/** Matter endpoint ids allow only [A-Za-z0-9_]; DIDA entity ids carry ':' and '-'. */
export const slug = (s: string) => s.replace(/[^a-zA-Z0-9_]/g, "_");

/** DIDA brightness (0-100 %) -> Matter level (1-254). Never 0: level 0 means "off"
 *  to a Matter controller, which is the on/off cluster's job, not the level's. */
export const pctToLevel = (pct: number) => Math.max(1, Math.min(254, Math.round((pct / 100) * 254)));

/** Matter level (0-254) -> DIDA brightness (0-100 %). */
export const levelToPct = (lvl: number) => Math.max(0, Math.min(100, Math.round((lvl / 254) * 100)));

export function prettify(s: string): string {
    return s.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim()
        .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Device label = the entity's friendly name (NO "DIDA" prefix — they're called by
 *  their real names via the assistant). Slug-like names are prettified to Title
 *  Case; already-nice names pass through unchanged. */
export function niceName(name: string | null, entityId: string): string {
    const base = name && name.trim() ? name.trim() : (entityId.split(":").pop() ?? entityId);
    if (!/\s/.test(base) && (/[-_]/.test(base) || base === base.toLowerCase())) return prettify(base);
    return base;
}

/** Which source option means "off" for an activity switch, spelling-insensitively. */
export const isPowerOff = (opt: string) => opt.replace(/[\s_-]/g, "").toLowerCase() === "poweroff";

// Only off/cool/heat cross to Matter; fan_only and friends stay in the DIDA UI.
export const HVAC_TO_SYSTEMMODE: Record<string, number> = { off: 0, cool: 3, heat: 4 };
export const SYSTEMMODE_TO_HVAC: Record<number, string> = { 0: "off", 3: "cool", 4: "heat" };

export const CLIMATE_MIN_C = 16, CLIMATE_MAX_C = 30;
export const c100 = (c: number) => Math.round(c * 100);              // °C -> Matter centi-°C (int16)
export const fromC100 = (v: number) => Math.round((v / 100) * 10) / 10; // centi-°C -> °C, 0.1 step
export const clampC = (c: number) => Math.max(CLIMATE_MIN_C, Math.min(CLIMATE_MAX_C, c));

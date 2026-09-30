// First tests for the Matter bridge. These conversions are the last thing that
// runs before a real device moves on a voice command, and a bug here has no
// DIDA-side symptom — the state stays right while the device does the wrong thing.
import { describe, expect, it } from "vitest";
import {
    CLIMATE_MAX_C, CLIMATE_MIN_C, HVAC_TO_SYSTEMMODE, SYSTEMMODE_TO_HVAC,
    c100, clampC, fromC100, isPowerOff, levelToPct, niceName, pctToLevel, prettify, slug,
} from "./mapping.js";

describe("brightness", () => {
    it("never emits level 0 — that means OFF to a controller, which is on/off's job", () => {
        expect(pctToLevel(0)).toBe(1);
        expect(pctToLevel(-5)).toBe(1);
    });
    it("maps the full range and clamps above it", () => {
        expect(pctToLevel(100)).toBe(254);
        expect(pctToLevel(200)).toBe(254);
        expect(pctToLevel(50)).toBe(127);
    });
    it("round-trips within a percent", () => {
        for (const pct of [1, 10, 25, 50, 75, 99, 100]) {
            expect(Math.abs(levelToPct(pctToLevel(pct)) - pct)).toBeLessThanOrEqual(1);
        }
    });
    it("clamps a level back into 0-100", () => {
        expect(levelToPct(0)).toBe(0);
        expect(levelToPct(254)).toBe(100);
        expect(levelToPct(999)).toBe(100);
    });
});

describe("climate", () => {
    it("round-trips °C through Matter centi-°C at 0.1 resolution", () => {
        for (const c of [16, 18.5, 21, 23.4, 30]) expect(fromC100(c100(c))).toBeCloseTo(c, 1);
    });
    it("clamps a setpoint to the exposed range", () => {
        expect(clampC(5)).toBe(CLIMATE_MIN_C);
        expect(clampC(45)).toBe(CLIMATE_MAX_C);
        expect(clampC(21)).toBe(21);
    });
    it("maps only the modes Matter understands, both ways", () => {
        for (const [hvac, sysmode] of Object.entries(HVAC_TO_SYSTEMMODE)) {
            expect(SYSTEMMODE_TO_HVAC[sysmode]).toBe(hvac); // no lossy pair
        }
        expect(HVAC_TO_SYSTEMMODE["fan_only"]).toBeUndefined(); // stays in the DIDA UI
    });
});

describe("endpoint ids", () => {
    it("strips everything Matter disallows", () => {
        expect(slug("mqtt:living-room:strip")).toBe("mqtt_living_room_strip");
        expect(slug("ok_id9")).toBe("ok_id9");
    });
    it("keeps distinct entities distinct", () => {
        expect(slug("a:b")).not.toBe(slug("a:c"));
    });
});

describe("naming", () => {
    it("prettifies a slug but leaves a real name alone", () => {
        expect(niceName(null, "mqtt:living-room-strip")).toBe("Living Room Strip");
        expect(niceName("Kuhinja gore", "x:y")).toBe("Kuhinja gore");
        expect(prettify("pmis_bea_room")).toBe("Pmis Bea Room");
    });
    it("falls back to the entity's last segment when unnamed", () => {
        expect(niceName("", "shelly:hallway")).toBe("Hallway");
        expect(niceName("   ", "shelly:hallway")).toBe("Hallway");
    });
});

describe("activity power-off detection", () => {
    it("matches however the option is spelled", () => {
        for (const s of ["Power off", "power_off", "POWER-OFF", "poweroff"]) expect(isPowerOff(s)).toBe(true);
        expect(isPowerOff("Power on")).toBe(false);
        expect(isPowerOff("Kodi")).toBe(false);
    });
});

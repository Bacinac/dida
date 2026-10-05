import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Endpoint, Environment, ServerNode, VendorId } from "@matter/main";
import { BridgedDeviceBasicInformationServer, ThermostatServer, WindowCoveringServer } from "@matter/main/behaviors";
import { WindowCovering } from "@matter/main/clusters";
import { DimmableLightDevice, OnOffPlugInUnitDevice, ThermostatDevice, WindowCoveringDevice } from "@matter/main/devices";
import { AggregatorEndpoint } from "@matter/main/endpoints";
import { describe, expect, it, vi } from "vitest";

import { fromController, MatterState, StateMirror, type StateUpdate } from "./state-sync.js";

const state = (value: unknown, capability = "on_off", entity_id = "mqtt:light"): StateUpdate => ({ entity_id, capability, value });

function deferred() {
    let resolve!: () => void;
    const promise = new Promise<void>((done) => { resolve = done; });
    return { promise, resolve };
}

describe("snapshot and live state ordering", () => {
    it("recovers a change missed during an outage without a new live report", async () => {
        const apply = vi.fn(async (_update: StateUpdate) => {});
        const mirror = new StateMirror(apply, vi.fn());
        await mirror.restore([state(false)], mirror.begin());
        mirror.invalidate();
        expect(mirror.synchronized).toBe(false);
        await mirror.restore([state(true)], mirror.begin());
        expect(apply.mock.calls.map(([update]) => update.value)).toEqual([false, true]);
        expect(mirror.synchronized).toBe(true);
    });

    it("keeps startup reports received while loading the snapshot and constructing endpoints", async () => {
        const apply = vi.fn(async (_update: StateUpdate) => {});
        const mirror = new StateMirror(apply, vi.fn());
        const generation = mirror.begin();
        mirror.receive(state(true));
        mirror.receive(state(75, "brightness"));
        expect(apply).not.toHaveBeenCalled();
        await mirror.restore([state(false), state(20, "brightness")], generation);
        expect(apply.mock.calls.map(([update]) => update)).toEqual([state(true), state(75, "brightness")]);
    });

    it("preserves a newer live delta arriving while an older snapshot write is in flight", async () => {
        const writing = deferred();
        const release = deferred();
        const values: unknown[] = [];
        const mirror = new StateMirror(async (update) => {
            if (update.value === false) {
                writing.resolve();
                await release.promise;
            }
            values.push(update.value);
        }, vi.fn());
        const restoring = mirror.restore([state(false)], mirror.begin());
        await writing.promise;
        mirror.receive(state(true));
        release.resolve();
        await restoring;
        expect(values).toEqual([false, true]);
        expect(mirror.synchronized).toBe(true);
    });

    it("discards a snapshot from an earlier connection generation", async () => {
        const apply = vi.fn(async (_update: StateUpdate) => {});
        const mirror = new StateMirror(apply, vi.fn());
        const oldGeneration = mirror.begin();
        mirror.invalidate();
        expect(await mirror.restore([state(false)], oldGeneration)).toBe(false);
        expect(apply).not.toHaveBeenCalled();
        expect(mirror.synchronized).toBe(false);
        await mirror.restore([state(true)], mirror.begin());
        expect(apply).toHaveBeenCalledExactlyOnceWith(state(true));
    });

    it("keeps health unsynchronized after a failed snapshot write and permits retry", async () => {
        const apply = vi.fn(async (_update: StateUpdate) => {}).mockRejectedValueOnce(new Error("write failed"));
        const mirror = new StateMirror(apply, vi.fn());
        await expect(mirror.restore([state(true)], mirror.begin())).rejects.toThrow("write failed");
        expect(mirror.synchronized).toBe(false);
        expect(await mirror.restore([state(true)], mirror.begin())).toBe(true);
    });
});

it("restores Matter attributes and activity groups without echoing device commands", async () => {
    const storage = mkdtempSync(join(tmpdir(), "dida-matter-sync-"));
    const environment = new Environment("state-sync-test", Environment.default);
    environment.vars.set("storage.path", storage);
    const server = await ServerNode.create({
        id: "state-sync-test", environment,
        productDescription: { name: "Test Bridge", deviceType: AggregatorEndpoint.deviceType },
        basicInformation: {
            vendorId: VendorId(0xfff1), productId: 0x8000, vendorName: "DIDA", productName: "Test Bridge",
            hardwareVersion: 1, softwareVersion: 1,
        },
    });
    try {
        const aggregator = new Endpoint(AggregatorEndpoint, { id: "aggregator" });
        await server.add(aggregator);
        const light = new Endpoint(DimmableLightDevice.with(BridgedDeviceBasicInformationServer), {
            id: "light", onOff: { onOff: false }, levelControl: { currentLevel: 1 },
            bridgedDeviceBasicInformation: { nodeLabel: "Light", reachable: true },
        });
        const climate = new Endpoint(ThermostatDevice.with(ThermostatServer.with("Heating", "Cooling"), BridgedDeviceBasicInformationServer), {
            id: "climate", bridgedDeviceBasicInformation: { nodeLabel: "Climate", reachable: true },
            thermostat: { systemMode: 0, controlSequenceOfOperation: 4, occupiedCoolingSetpoint: 2400, occupiedHeatingSetpoint: 2400 },
        });
        const activity = new Endpoint(OnOffPlugInUnitDevice.with(BridgedDeviceBasicInformationServer), {
            id: "activity", onOff: { onOff: false },
            bridgedDeviceBasicInformation: { nodeLabel: "Activity", reachable: true },
        });
        const previousActivity = new Endpoint(OnOffPlugInUnitDevice.with(BridgedDeviceBasicInformationServer), {
            id: "previous-activity", onOff: { onOff: true },
            bridgedDeviceBasicInformation: { nodeLabel: "Previous Activity", reachable: true },
        });
        const cover = new Endpoint(WindowCoveringDevice.with(WindowCoveringServer.with("Lift", "PositionAwareLift"), BridgedDeviceBasicInformationServer), {
            id: "cover", bridgedDeviceBasicInformation: { nodeLabel: "Cover", reachable: true },
            windowCovering: {
                type: WindowCovering.WindowCoveringType.Rollershade,
                currentPositionLiftPercent100ths: 10000, targetPositionLiftPercent100ths: 10000,
            },
        });
        await aggregator.add(light);
        await aggregator.add(climate);
        await aggregator.add(activity);
        await aggregator.add(previousActivity);
        await aggregator.add(cover);
        const commands = vi.fn();
        const changed = vi.fn();
        const observe = (_value: unknown, _old: unknown, context: unknown) => {
            changed();
            if (fromController(context)) commands();
        };
        light.events.onOff.onOff$Changed.on(observe);
        light.events.levelControl.currentLevel$Changed.on(observe);
        climate.events.thermostat.systemMode$Changed.on(observe);
        climate.events.thermostat.occupiedCoolingSetpoint$Changed.on(observe);
        climate.events.thermostat.occupiedHeatingSetpoint$Changed.on(observe);
        activity.events.onOff.onOff$Changed.on(observe);
        previousActivity.events.onOff.onOff$Changed.on(observe);
        const target = new MatterState();
        target.endpoints.set("mqtt:light", light);
        target.endpoints.set("mqtt:climate", climate);
        target.climateIds.add("mqtt:climate");
        target.endpoints.set("virtual:gate", cover);
        target.coverIds.add("virtual:gate");
        target.activityEndpoints.set("harmony:hub", new Map([["PS5", activity], ["TV", previousActivity]]));
        const mirror = new StateMirror((update) => target.apply(update), vi.fn());
        await mirror.restore([
            state(true), state(50, "brightness"),
            state("heat", "hvac_mode", "mqtt:climate"),
            state(21, "target_temperature", "mqtt:climate"),
            state(22.5, "temperature", "mqtt:climate"),
            state(true, "on_off", "virtual:gate"),
            state("PS5", "source", "harmony:hub"),
        ], mirror.begin());
        expect(light.state.onOff.onOff).toBe(true);
        expect(light.state.levelControl.currentLevel).toBe(127);
        expect(climate.state.thermostat.systemMode).toBe(4);
        expect(climate.state.thermostat.occupiedCoolingSetpoint).toBe(2100);
        expect(climate.state.thermostat.occupiedHeatingSetpoint).toBe(2100);
        expect(climate.state.thermostat.externalMeasuredIndoorTemperature).toBe(2250);
        expect(cover.state.windowCovering.currentPositionLiftPercent100ths).toBe(0);
        expect(cover.state.windowCovering.targetPositionLiftPercent100ths).toBe(0);
        expect(activity.state.onOff.onOff).toBe(true);
        expect(previousActivity.state.onOff.onOff).toBe(false);
        expect(target.activityCurrent.get("harmony:hub")).toBe("PS5");
        expect(changed.mock.calls.length).toBeGreaterThanOrEqual(6);
        expect(commands).not.toHaveBeenCalled();
        observe(true, false, { fabric: {} });
        expect(commands).toHaveBeenCalledOnce();
    } finally {
        await server.close();
        rmSync(storage, { recursive: true, force: true });
    }
});

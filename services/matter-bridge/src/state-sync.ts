import type { Endpoint } from "@matter/main";

import { c100, clampC, HVAC_TO_SYSTEMMODE, pctToLevel } from "./mapping.js";

export type StateUpdate = { entity_id: string; capability: string; value: unknown };
type ApplyState = (update: StateUpdate) => Promise<void>;

export function fromController(context: unknown): boolean {
    return Boolean((context as { fabric?: unknown } | undefined)?.fabric);
}

export class StateMirror {
    synchronized = false;
    private generation = 0;
    private pending = new Map<string, StateUpdate>();
    private queue: Promise<unknown> = Promise.resolve();

    constructor(private apply: ApplyState, private failed: (error: unknown) => void) {}

    begin(): number {
        this.synchronized = false;
        this.pending.clear();
        return ++this.generation;
    }

    invalidate(): void {
        this.synchronized = false;
        ++this.generation;
    }

    current(generation: number): boolean {
        return generation === this.generation;
    }

    receive(update: StateUpdate): void {
        if (!this.synchronized) {
            this.pending.set(`${update.entity_id}\0${update.capability}`, update);
            return;
        }
        const generation = this.generation;
        this.queue = this.queue.then(async () => {
            if (generation === this.generation) await this.apply(update);
        }).catch((error) => {
            this.invalidate();
            this.failed(error);
        });
    }

    async restore(states: StateUpdate[], generation: number): Promise<boolean> {
        const job = this.queue.then(async () => {
            for (const state of states) {
                if (generation !== this.generation) return false;
                if (!this.pending.has(`${state.entity_id}\0${state.capability}`)) await this.apply(state);
            }
            while (this.pending.size) {
                const updates = [...this.pending.values()];
                this.pending.clear();
                for (const update of updates) {
                    if (generation !== this.generation) return false;
                    await this.apply(update);
                }
            }
            if (generation !== this.generation) return false;
            this.synchronized = true;
            return true;
        });
        this.queue = job.catch(() => {});
        return job;
    }
}

export class MatterState {
    readonly endpoints = new Map<string, Endpoint>();
    readonly climateIds = new Set<string>();
    readonly coverIds = new Set<string>();
    readonly activityEndpoints = new Map<string, Map<string, Endpoint>>();
    readonly activityCurrent = new Map<string, string>();

    async apply(u: StateUpdate): Promise<void> {
        const perOption = this.activityEndpoints.get(u.entity_id);
        if (perOption && u.capability === "source") {
            const current = String(u.value);
            this.activityCurrent.set(u.entity_id, current);
            for (const [option, ep] of perOption) {
                await ep.setStateOf("onOff", { onOff: option === current });
            }
            return;
        }
        const endpoint = this.endpoints.get(u.entity_id);
        if (!endpoint) return;
        if (this.coverIds.has(u.entity_id) && u.capability === "on_off") {
            const lift = u.value ? 0 : 10000;
            await endpoint.setStateOf("windowCovering", { currentPositionLiftPercent100ths: lift, targetPositionLiftPercent100ths: lift });
        } else if (u.capability === "on_off") {
            await endpoint.setStateOf("onOff", { onOff: Boolean(u.value) });
        } else if (u.capability === "brightness") {
            await endpoint.setStateOf("levelControl", { currentLevel: pctToLevel(Number(u.value)) });
        } else if (this.climateIds.has(u.entity_id) && u.capability === "hvac_mode") {
            const mode = HVAC_TO_SYSTEMMODE[String(u.value)];
            if (mode !== undefined) await endpoint.setStateOf("thermostat", { systemMode: mode });
        } else if (this.climateIds.has(u.entity_id) && u.capability === "target_temperature") {
            const value = c100(clampC(Number(u.value)));
            await endpoint.setStateOf("thermostat", { occupiedCoolingSetpoint: value, occupiedHeatingSetpoint: value });
        } else if (this.climateIds.has(u.entity_id) && u.capability === "temperature") {
            // The server derives localTemperature from this external reading.
            await endpoint.setStateOf("thermostat", { externalMeasuredIndoorTemperature: c100(Number(u.value)) });
        }
    }
}

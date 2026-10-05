import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { decode, encode } from "@msgpack/msgpack";
import { Endpoint, Environment, ServerNode, VendorId } from "@matter/main";
import { BridgedDeviceBasicInformationServer, ThermostatServer, WindowCoveringServer } from "@matter/main/behaviors";
import { DimmableLightDevice, OnOffLightDevice, OnOffPlugInUnitDevice, ThermostatDevice, WindowCoveringDevice } from "@matter/main/devices";
import { AggregatorEndpoint } from "@matter/main/endpoints";
import { WindowCovering } from "@matter/main/clusters";
import { connect } from "@nats-io/transport-node";

import { Broker, ownKey } from "./broker.js";
import { fromController, MatterState, StateMirror, type StateUpdate } from "./state-sync.js";

// ---- health marker (compose healthcheck) -----------------------------------
// The periodic touch is gated on NATS connectivity (installed after connect,
// below) so a dead bus makes the healthcheck FAIL instead of reporting healthy
// while the state mirror is silently down.
const HEALTH = "/tmp/dida_healthy_matter-bridge";
const touch = () => { try { writeFileSync(HEALTH, "ok"); } catch { /* ignore */ } };

// Set once SIGTERM/SIGINT arrives so the various "connection closed → exit(1)"
// guards below don't fight the clean shutdown path (which exits 0).
let shuttingDown = false;

import {
    CLIMATE_MAX_C, CLIMATE_MIN_C, HVAC_TO_SYSTEMMODE, SYSTEMMODE_TO_HVAC,
    c100, clampC, fromC100, isPowerOff, levelToPct, niceName, pctToLevel, slug,
} from "./mapping.js";

// ---- helpers ----------------------------------------------------------------
const env = process.env;

// ---- DIDA links: the broker (snapshot) + NATS (live state + commands) --------
const key = ownKey();

// What reaches Google/Matter is decided by `entities.voice_effective` — a column
// Postgres computes from a RULE (a controllable light/switch/cover) plus an
// optional per-entity override in `voice_exposed`. The rule lives in the database
// precisely so this file and the Svelte UI do not each restate it; a new device
// appears in Google Home on its own, and curation is only for the exceptions.
// The override is the reason a raw pulse relay (`...:gate`, which tapped
// directly just flicks once and confuses the gate controller) stays hidden while
// the sequenced `virtual:*_gate` helper is exposed — both are switches, only the
// flag tells them apart. It replaced DIDA_MATTER_EXCLUDE, which matched by exact
// entity_id and silently broke when an ESPHome slug drifted.

// Entities whose `source` options become one Matter switch PER OPTION — the
// voice path for activity pickers (Harmony hub: "turn on PS5" starts that
// activity). Matter has no controller-voiced mode-select, so each activity is a
// plug: exactly one is on (the current source), turning another on switches to
// it, turning the active one off selects the power-off option. Plugs, not
// lights, so "turn off the lights" can never tear down the AV chain.
// Deployment-specific → environment (DIDA_MATTER_ACTIVITY_ENTITIES, CSV).
const MATTER_ACTIVITY_ENTITIES = (process.env.DIDA_MATTER_ACTIVITY_ENTITIES ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

// ---- gates exposed as window coverings (so voice says "open/close") ---------
// A DIDA gate (device_type='cover' with on_off) is a MOMENTARY pulse that
// self-closes: the automation runs press-wait-press and resets the virtual
// control. Exposing it as a Matter window covering gives Google the natural
// "open the gate" verb instead of "turn on". OPEN fires the DIDA sequence; CLOSE
// is a no-op (the gate closes itself). Real open/closed comes back over
// dida.events (the virtual entity's on_off) and drives the lift position, so the
// tile follows the sequence (open while the pulse runs, closed after) rather than
// sticking open. Matter lift %: 0 = fully open, 10000 = fully closed.
const LIFT_OPEN = 0, LIFT_CLOSED = 10000;
// endpoint slug -> entity id, so a covering command handler (which only knows its
// own endpoint) can publish to the right DIDA entity.
const coverEntityByEndpoint = new Map<string, string>();

// What the api's `voice` op answers: per kind, the entities and their starting values.
type Kind = {
    entities: Array<{ entity_id: string; name: string | null; device_type: string | null }>;
    states: StateUpdate[];
};
type Voice = { controllables: Kind; climates: Kind; covers: Kind; activities: Kind };

function loadVoice(): Promise<Voice> {
    return broker.call<Voice>("voice", { activities: MATTER_ACTIVITY_ENTITIES });
}

// Lights and switches; gates and climates come as coverings and thermostats.
function controllablesOf({ controllables: { entities, states } }: Voice): Map<string, { name: string | null; light: boolean; onOff: boolean; brightness: number | null }> {
    const state = new Map<string, { name: string | null; light: boolean; onOff: boolean; brightness: number | null }>();
    for (const e of entities) state.set(e.entity_id, { name: e.name, light: e.device_type === "light", onOff: false, brightness: null });
    for (const s of states) {
        const cur = state.get(s.entity_id);
        if (!cur) continue;
        if (s.capability === "on_off") cur.onOff = Boolean(s.value);
        else if (s.capability === "brightness") cur.brightness = Number(s.value);
    }
    return state;
}

// ---- activity pickers (source -> one switch per option) ---------------------
function activitiesOf({ activities: { entities, states } }: Voice): Map<string, { name: string | null; current: string; options: string[]; powerOff: string | null }> {
    const out = new Map<string, { name: string | null; current: string; options: string[]; powerOff: string | null }>();
    for (const e of entities) out.set(e.entity_id, { name: e.name, current: "", options: [], powerOff: null });
    for (const s of states) {
        const cur = out.get(s.entity_id);
        if (!cur) continue;
        if (s.capability === "source") cur.current = String(s.value);
        else if (s.capability === "source_options") {
            // source_options is a JSON-encoded list; a malformed value is a real
            // contract bug on the adapter side — log loud, expose nothing.
            try {
                const opts = JSON.parse(String(s.value));
                if (Array.isArray(opts)) cur.options = opts.map(String);
            } catch (err) {
                console.warn(`[matter] bad source_options for ${s.entity_id}:`, (err as Error).message);
            }
        }
    }
    for (const [eid, info] of out) {
        info.powerOff = info.options.find(isPowerOff) ?? null;
        info.options = info.options.filter((o) => !isPowerOff(o));
        if (!info.options.length) {
            console.warn(`[matter] ${eid} configured as activity entity but has no source options — skipping`);
            out.delete(eid);
        }
    }
    return out;
}

// ---- climate (AC / thermostat) ----------------------------------------------
// DIDA climate decomposes into scalar caps; Matter wants one Thermostat cluster.
// We expose Heating+Cooling (NOT AutoMode — its setpoint deadband cross-check
// adds friction to the primary job: setting a single target). The AC's auto/dry/
// fan_only modes stay in the DIDA UI; off/cool/heat + setpoint are what voice
// needs. A single DIDA target mirrors to both heat+cool setpoints; whichever the
// controller adjusts, we publish the one set_temperature.


function climatesOf({ climates: { entities, states } }: Voice): Map<string, { name: string | null; mode: string; target: number; current: number | null }> {
    const out = new Map<string, { name: string | null; mode: string; target: number; current: number | null }>();
    for (const e of entities) out.set(e.entity_id, { name: e.name, mode: "off", target: 24, current: null });
    for (const s of states) {
        const cur = out.get(s.entity_id);
        if (!cur) continue;
        if (s.capability === "hvac_mode") cur.mode = String(s.value);
        else if (s.capability === "target_temperature") cur.target = Number(s.value);
        else if (s.capability === "temperature") cur.current = Number(s.value);
    }
    return out;
}

// ---- covers (gates) ---------------------------------------------------------
function coversOf({ covers: { entities, states } }: Voice): Map<string, { name: string | null; onOff: boolean }> {
    const out = new Map<string, { name: string | null; onOff: boolean }>();
    for (const e of entities) out.set(e.entity_id, { name: e.name, onOff: false });
    for (const s of states) { const c = out.get(s.entity_id); if (c && s.capability === "on_off") c.onOff = Boolean(s.value); }
    return out;
}

// The bridge logs into the bus as itself, with the password the keys service wrote
// beside its key (dida_core.identity); replies come back on its own inboxes, the
// only ones the server lets it hear.
const natsUri = new URL(env.DIDA_NATS_URL ?? "nats://127.0.0.1:4232");
const natsAuth = env.DIDA_NATS_PASSWORD_FILE
    ? { user: "matter-bridge", pass: readFileSync(env.DIDA_NATS_PASSWORD_FILE, "utf8").trim() }
    : {};
// Infinite reconnect (default is 10 attempts → a >~25s NATS outage would silently
// and permanently kill the state mirror). If the connection ever DOES close for
// good, exit so compose restarts us rather than running blind.
const nc = await connect({
    servers: `${natsUri.hostname}:${natsUri.port || 4222}`,
    ...natsAuth,
    inboxPrefix: "_INBOX.matter-bridge",
    maxReconnectAttempts: -1,
    waitOnFirstConnect: true,
});
const broker = new Broker(nc, "matter-bridge", key);
const matterState = new MatterState();
const { endpoints, climateIds, coverIds, activityEndpoints, activityCurrent } = matterState;
const mirror = new StateMirror((update) => matterState.apply(update), (error) => {
    console.warn("[matter] failed to apply live state:", (error as Error).message);
    requestSync();
});
let bridgeReady = false;
let syncRequested = false;
let syncTask: Promise<void> | undefined;
const bootGeneration = mirror.begin();
nc.subscribe("dida.events", {
    callback: (error, message) => {
        if (error) {
            mirror.invalidate();
            console.warn("[matter] live subscription failed:", error.message);
            requestSync();
            return;
        }
        try {
            const update = decode(message.data) as Partial<StateUpdate>;
            if (typeof update.entity_id === "string" && typeof update.capability === "string") {
                mirror.receive(update as StateUpdate);
            }
        } catch (error) {
            console.warn("[matter] failed to decode dida.events frame:", (error as Error).message);
        }
    },
});
nc.closed().then((err) => {
    if (shuttingDown) return;  // clean shutdown drains nc on purpose — not a fault
    console.error("[matter] NATS connection closed — exiting for restart", err ?? "");
    process.exit(1);
});
// Track live bus reachability. With maxReconnectAttempts:-1 the connection never
// CLOSES during an outage (so isClosed() stays false), so we can't lean on that —
// we watch the status stream instead: disconnect/reconnecting/staleConnection mean
// the mirror is down, reconnect means it's back. Starts up (waitOnFirstConnect).
let busUp = true;
(async () => {
    for await (const s of nc.status()) {
        if (s.type === "disconnect" || s.type === "reconnecting" || s.type === "staleConnection") {
            busUp = false;
            mirror.invalidate();
        } else if (s.type === "reconnect") {
            busUp = true;
            requestSync();
        }
    }
})();
// Health reflects NATS connectivity: while the bus is down the touch stops → the
// healthcheck marker goes stale → compose fails it (instead of reporting healthy
// while the state mirror is silently disconnected).
setInterval(() => { if (bridgeReady && busUp && mirror.synchronized && !nc.isClosed()) touch(); }, 5000);

function publishCommand(entityId: string, capability: string, command: string, args: Record<string, unknown> = {}) {
    // source: the audit-trail identity. Matter can't attribute further (Google/
    // Alexa/Apple controllers all arrive over the same fabric), so "matter" it is.
    const payload = encode(
        { entity_id: entityId, capability, command, ts_ns: BigInt(Date.now()) * 1_000_000n, args, source: "matter" },
        { useBigInt64: true },
    );
    nc.publish(`dida.command.${entityId.split(":")[0]}.${entityId}`, payload);
}

// Window-covering server for a DIDA gate. OPEN (or a mostly-open lift %) fires the
// gate's DIDA sequence via a turn_on command; CLOSE/STOP are no-ops (the gate
// self-closes). We snap the lift to the commanded end so the controller gets
// instant feedback; the authoritative open/closed then arrives over dida.events.
// `this.endpoint.id` is the slug — reverse-mapped to the entity id to publish.
class GateCoverServer extends WindowCoveringServer.with("Lift", "PositionAwareLift") {
    #fire(): void {
        const eid = coverEntityByEndpoint.get(this.endpoint.id);
        if (eid) publishCommand(eid, "on_off", "turn_on");
    }
    override async upOrOpen(): Promise<void> {
        this.#fire();
        this.state.currentPositionLiftPercent100ths = LIFT_OPEN;
        this.state.targetPositionLiftPercent100ths = LIFT_OPEN;
    }
    override async downOrClose(): Promise<void> {
        this.state.currentPositionLiftPercent100ths = LIFT_CLOSED;
        this.state.targetPositionLiftPercent100ths = LIFT_CLOSED;
    }
    override async goToLiftPercentage(req: { liftPercent100thsValue: number }): Promise<void> {
        if (req.liftPercent100thsValue <= 5000) await this.upOrOpen();
        else await this.downOrClose();
    }
    override async stopMotion(): Promise<void> { /* momentary pulse — nothing to stop */ }
}

// ---- Matter bridge ----------------------------------------------------------
const environment = Environment.default;
const STORAGE = env.DIDA_MATTER_STORAGE ?? "/state";
environment.vars.set("storage.path", STORAGE);

// matter.js guards its store with a lockfile naming the writer's pid, and
// refuses to start while one exists. That pid means nothing to a container that
// just started: fresh PID namespace, and this volume has exactly one writer. So
// a lock found here is always the residue of a run that was killed instead of
// drained — leave it and the bridge crash-loops until a human deletes the file
// (it did, on Cabin). Clear it, but say so: a clean shutdown never leaves one.
const lock = join(STORAGE, "dida-bridge", "matter.lock");
if (existsSync(lock)) {
    console.warn(`matter: stale storage lock at ${lock} — previous run did not shut down cleanly; clearing`);
    rmSync(lock, { force: true });
}

await nc.flush();
const voice = await loadVoice();
const bootSig = signatureOf(voice);
// A controller's subscription names the attributes of the endpoints it knew, and
// matter.js restores it after a restart. Restored over a changed set, it leaves the
// new endpoints with no reports and Google shows them offline, never resubscribing
// on its own. Only a subscription made against the same set is worth restoring.
const SIGNATURE = join(STORAGE, "exposed.sig");
const sameSet = existsSync(SIGNATURE) && readFileSync(SIGNATURE, "utf8") === bootSig;
if (!sameSet) console.log("[matter] exposed device set changed since the last run — controllers subscribe afresh");

const server = await ServerNode.create({
    id: "dida-bridge",
    subscriptions: { persistenceEnabled: sameSet },
    network: { port: Number(env.DIDA_MATTER_PORT ?? 5540) },
    productDescription: { name: "DIDA Bridge", deviceType: AggregatorEndpoint.deviceType },
    basicInformation: {
        vendorName: "DIDA", vendorId: VendorId(0xfff1),
        productName: "DIDA Bridge", productLabel: "Bridge", productId: 0x8000, nodeLabel: "DIDA",
        hardwareVersion: 1, softwareVersion: 1,
    },
});

const aggregator = new Endpoint(AggregatorEndpoint, { id: "aggregator" });
await server.add(aggregator);

const controllables = controllablesOf(voice);
for (const [entityId, info] of controllables) {
    const id = slug(entityId);
    const label = niceName(info.name, entityId);
    const dimmable = info.brightness !== null;

    const dimmer = dimmable
        ? new Endpoint(DimmableLightDevice.with(BridgedDeviceBasicInformationServer), {
              id,
              bridgedDeviceBasicInformation: { nodeLabel: label, reachable: true },
              onOff: { onOff: info.onOff },
              levelControl: { currentLevel: pctToLevel(info.brightness ?? 0) },
          })
        : null;
    // Google counts every light in "turn off all the lights": a plug, a UPS or a
    // switch that runs a routine must not be one.
    const endpoint = dimmer ?? new Endpoint((info.light ? OnOffLightDevice : OnOffPlugInUnitDevice).with(BridgedDeviceBasicInformationServer), {
        id,
        bridgedDeviceBasicInformation: { nodeLabel: label, reachable: true },
        onOff: { onOff: info.onOff },
    });
    await aggregator.add(endpoint);
    endpoints.set(entityId, endpoint);

    // Controller -> DIDA. `context.fabric` is set only for a real controller
    // action; our own attribute writes (from DIDA state) run offline, so this
    // guard avoids an echo loop.
    endpoint.events.onOff.onOff$Changed.on((value, _old, context) => {
        if (!fromController(context)) return;
        publishCommand(entityId, "on_off", value ? "turn_on" : "turn_off");
    });
    dimmer?.events.levelControl.currentLevel$Changed.on((level, _old, context) => {
        if (!fromController(context)) return;
        publishCommand(entityId, "brightness", "set_brightness", { value: levelToPct(Number(level)) });
    });
}

// ---- cover endpoints (gates as window coverings) ----------------------------
// The controller->DIDA path lives in GateCoverServer's overridden commands (open
// fires the sequence); here we just create the endpoint seeded with the current
// open/closed lift. Minimal state — matter.js fills mode/status/limits defaults;
// only `type` + the two lift positions are required with Lift+PositionAwareLift.
for (const [entityId, info] of coversOf(voice)) {
    const id = slug(entityId);
    const label = niceName(info.name, entityId);
    coverEntityByEndpoint.set(id, entityId);
    const lift = info.onOff ? LIFT_OPEN : LIFT_CLOSED;
    const endpoint = new Endpoint(WindowCoveringDevice.with(GateCoverServer, BridgedDeviceBasicInformationServer), {
        id,
        bridgedDeviceBasicInformation: { nodeLabel: label, reachable: true },
        windowCovering: {
            type: WindowCovering.WindowCoveringType.Rollershade,
            currentPositionLiftPercent100ths: lift,
            targetPositionLiftPercent100ths: lift,
        },
    });
    await aggregator.add(endpoint);
    endpoints.set(entityId, endpoint);
    coverIds.add(entityId);
}

// ---- climate endpoints (Thermostat) -----------------------------------------
const ThermostatHC = ThermostatServer.with("Heating", "Cooling");
for (const [entityId, info] of climatesOf(voice)) {
    const id = slug(entityId);
    const label = niceName(info.name, entityId);
    const target = c100(clampC(info.target));
    const endpoint = new Endpoint(
        ThermostatDevice.with(ThermostatHC, BridgedDeviceBasicInformationServer),
        {
            id,
            bridgedDeviceBasicInformation: { nodeLabel: label, reachable: true },
            thermostat: {
                systemMode: HVAC_TO_SYSTEMMODE[info.mode] ?? 0,
                controlSequenceOfOperation: 4, // CoolingAndHeating
                localTemperature: info.current !== null ? c100(info.current) : null,
                externalMeasuredIndoorTemperature: info.current !== null ? c100(info.current) : undefined,
                occupiedCoolingSetpoint: target,
                occupiedHeatingSetpoint: target,
                absMinCoolSetpointLimit: c100(CLIMATE_MIN_C), absMaxCoolSetpointLimit: c100(CLIMATE_MAX_C),
                minCoolSetpointLimit: c100(CLIMATE_MIN_C), maxCoolSetpointLimit: c100(CLIMATE_MAX_C),
                absMinHeatSetpointLimit: c100(CLIMATE_MIN_C), absMaxHeatSetpointLimit: c100(CLIMATE_MAX_C),
                minHeatSetpointLimit: c100(CLIMATE_MIN_C), maxHeatSetpointLimit: c100(CLIMATE_MAX_C),
            },
        },
    );
    await aggregator.add(endpoint);
    endpoints.set(entityId, endpoint);
    climateIds.add(entityId);

    // Controller -> DIDA (guarded by context.fabric to avoid echoing our own
    // reflective writes). The AC has a single setpoint: mirror whichever side
    // the controller moved onto the other, and publish one set_temperature.
    endpoint.events.thermostat.systemMode$Changed.on((mode, _old, context) => {
        if (!fromController(context)) return;
        const hvac = SYSTEMMODE_TO_HVAC[Number(mode)];
        if (hvac) publishCommand(entityId, "hvac_mode", "set_hvac_mode", { value: hvac });
    });
    const onSetpoint = (other: "occupiedHeatingSetpoint" | "occupiedCoolingSetpoint") =>
        (value: number, _old: unknown, context: unknown) => {
            if (!fromController(context)) return;
            void endpoint.set({ thermostat: { [other]: value } }).catch(
                (e) => console.warn(`[matter] setpoint mirror ${entityId}:`, (e as Error).message),
            );
            publishCommand(entityId, "target_temperature", "set_temperature", { value: fromC100(Number(value)) });
        };
    endpoint.events.thermostat.occupiedCoolingSetpoint$Changed.on(onSetpoint("occupiedHeatingSetpoint"));
    endpoint.events.thermostat.occupiedHeatingSetpoint$Changed.on(onSetpoint("occupiedCoolingSetpoint"));
}

// ---- activity endpoints (one plug per source option) ------------------------
// activityEndpoints: entity -> (option -> endpoint); activityCurrent mirrors the
// entity's live `source` so the OFF handler knows whether it's tearing down the
// ACTIVE activity (-> power-off option) or a stale/no-op toggle.
const activityPowerOff = new Map<string, string | null>();

for (const [entityId, info] of activitiesOf(voice)) {
    const perOption = new Map<string, Endpoint>();
    activityEndpoints.set(entityId, perOption);
    activityCurrent.set(entityId, info.current);
    activityPowerOff.set(entityId, info.powerOff);

    for (const option of info.options) {
        const endpoint = new Endpoint(OnOffPlugInUnitDevice.with(BridgedDeviceBasicInformationServer), {
            id: slug(`${entityId}:${option}`),
            bridgedDeviceBasicInformation: { nodeLabel: option, reachable: true },
            onOff: { onOff: info.current === option },
        });
        await aggregator.add(endpoint);
        perOption.set(option, endpoint);

        endpoint.events.onOff.onOff$Changed.on((value, _old, context) => {
            if (!fromController(context)) return;
            if (value) {
                publishCommand(entityId, "source", "set_source", { value: option });
                return;
            }
            // OFF on the active activity selects the power-off option; OFF on an
            // inactive one is a stale toggle. Either way the real state comes
            // back through dida.events — but when there's nothing to publish
            // (no power-off option / inactive), re-assert ON ourselves so the
            // controller isn't left showing "off" for the still-running source.
            const powerOff = activityPowerOff.get(entityId);
            if (activityCurrent.get(entityId) === option && powerOff) {
                publishCommand(entityId, "source", "set_source", { value: powerOff });
            } else if (activityCurrent.get(entityId) === option) {
                void endpoint.set({ onOff: { onOff: true } }).catch(
                    (e) => console.warn(`[matter] re-assert ${entityId}/${option}:`, (e as Error).message),
                );
            }
        });
    }
}

await mirror.restore(Object.values(voice).flatMap((kind) => kind.states), bootGeneration);
await server.start();
bridgeReady = true;
if (!mirror.synchronized || syncRequested) requestSync();

const pc = server.state.commissioning.pairingCodes;
console.log("=".repeat(64));
const activityCount = [...activityEndpoints.values()].reduce((n, m) => n + m.size, 0);
console.log(`DIDA Matter bridge up — ${endpoints.size} device(s) + ${activityCount} activity switch(es) exposed.`);
console.log("Commission (if not yet): manual code", pc.manualPairingCode, "| QR", pc.qrPairingCode);
console.log("=".repeat(64));

// ---- device-set re-sync -----------------------------------------------------
// The exposed set is built once at boot. Newly added devices would never appear
// and removed ones would linger as ghost endpoints. matter.js storage persists
// across restarts (no re-pairing), so the simplest robust re-sync is: detect a
// change in the desired set and exit — compose restarts us and rebuilds cleanly.
function signatureOf(v: Voice): string {
    const ctrl = controllablesOf(v);
    const clim = climatesOf(v);
    const covs = coversOf(v);
    const acts = activitiesOf(v);
    const parts: string[] = [];
    for (const [eid, info] of ctrl) parts.push(`${eid}:${info.brightness !== null ? "d" : info.light ? "o" : "p"}`);
    for (const eid of clim.keys()) parts.push(`${eid}:c`);
    for (const eid of covs.keys()) parts.push(`${eid}:cover`);
    // Options are part of the signature: a renamed/added Harmony activity must
    // re-expose (restart), same as a device appearing or vanishing.
    for (const [eid, info] of acts) parts.push(`${eid}:a:${[...info.options].sort().join(",")}`);
    return parts.sort().join("|");
}
writeFileSync(SIGNATURE, bootSig);
function requestSync(): void {
    syncRequested = true;
    if (!bridgeReady || syncTask || !busUp || shuttingDown) return;
    syncTask = (async () => {
        while (syncRequested && busUp && !shuttingDown) {
            syncRequested = false;
            const generation = mirror.begin();
            try {
                await nc.flush();
                const snapshot = await loadVoice();
                if (!mirror.current(generation)) {
                    syncRequested = true;
                    continue;
                }
                if (signatureOf(snapshot) !== bootSig) {
                    console.log("[matter] exposed device set changed — exiting to re-expose (compose restart)");
                    process.exit(0);
                }
                if (!await mirror.restore(Object.values(snapshot).flatMap((kind) => kind.states), generation)) {
                    syncRequested = true;
                }
            } catch (error) {
                console.warn("[matter] state snapshot sync failed; retrying:", (error as Error).message);
                syncRequested = true;
                await new Promise((resolve) => setTimeout(resolve, 5000));
            }
        }
    })().finally(() => {
        syncTask = undefined;
        if (syncRequested && busUp && !shuttingDown) requestSync();
    });
}
setInterval(requestSync, 60_000);

// ---- graceful shutdown -------------------------------------------------------
// As PID 1 (host networking) Node gets NO default SIGTERM action, so without this
// `docker stop` waits the full stop-timeout then SIGKILLs — no NATS drain, no clean
// matter.js close. Close everything and exit 0 (compose `init: true` forwards
// the signal). Idempotent so a SIGINT-then-SIGTERM can't double-run it.
async function shutdown(sig: string): Promise<void> {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log(`[matter] ${sig} — shutting down`);
    try { await server.close(); } catch (e) { console.warn("[matter] server close:", (e as Error).message); }
    try { await nc.drain(); } catch (e) { console.warn("[matter] nats drain:", (e as Error).message); }
    process.exit(0);
}
process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));

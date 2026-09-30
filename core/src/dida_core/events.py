"""Wire types and subject conventions for the DIDA bus.

We use msgspec structs (msgpack on the wire) — same choice as BABA's frame
notifications: tiny, fast, schema-checked on decode.

Subjects (NATS):
    dida.state.<pub>.<entity_id>   adapter -> engine   device reported a value
    dida.entity.<pub>.<entity_id>  adapter -> engine   entity EXISTS (catalog metadata)
    dida.reachability.<pub>        adapter -> engine   a device's reachability verdict
    dida.heartbeat.<pub>           adapter -> engine   still here
    dida.journal.<pub>             *       -> journal  something happened
    dida.logs.<pub>                *       -> journal  a log line
    dida.command.<ns>.<entity_id>  api/automation -> adapter  act on a device;
                               an adapter hears only its own namespace <ns>
    dida.events                engine  -> *         validated state stream (api/automations)
    dida.status.<name>         adapter -> *         adapter liveness/health (see health.py)

entity_id is namespaced by adapter, e.g. "mqtt:kitchen_light", so it's unique
across protocols and trivially routable by the adapter that owns it.

<pub> is the publisher the message claims to be (`publisher`). The server lets an
adapter publish only under its own name (dida_core.identity), and every consumer
drops a message whose claim differs from its subject, so a claim is a fact.
"""

from __future__ import annotations

import msgspec

from dida_core.capabilities import Value

STATE_SUBJECT_PREFIX = "dida.state"
ENTITY_SUBJECT_PREFIX = "dida.entity"
COMMAND_SUBJECT_PREFIX = "dida.command"
ENGINE_EVENTS_SUBJECT = "dida.events"
# Discrete DEVICE EVENTS — "it went offline", "a rule fired", "an update was
# rejected" — as opposed to `dida.events`, which republishes measured state. Kept
# on its own subject so the live state stream stays exactly that: a house's
# journal is low-volume and diagnostic, and must never compete with projection.
JOURNAL_SUBJECT = "dida.journal"
LOGS_SUBJECT = "dida.logs"
# Whether a physical device is currently REACHABLE, as its adapter knows it — an
# ESPHome node's connection, a zigbee2mqtt availability report, a cloud device's
# online flag. Distinct from a state value: a device can hold a perfectly fresh
# last reading and be unreachable now (the bulbs on Cabin read "on" for four hours
# after they lost power). Device-level on purpose — reachability is a property of
# the hardware, so every entity of that device inherits it. Core NATS, not
# JetStream: it is current-status, not history; the latest word wins and a missed
# transition is corrected by the next one.
REACHABILITY_SUBJECT = "dida.reachability"
# A process's "still here", for every adapter namespace it has announced entities
# under. A device verdict comes from its adapter, so when the adapter itself dies
# nobody is left to say the device is gone; the engine hears the silence instead.
HEARTBEAT_SUBJECT = "dida.heartbeat"
HEARTBEAT_S = 15

# The publisher of a message from a core service that names none (a journal event
# with no source). No adapter is called this, so none can publish under it.
CORE = "core"

_NOT_IN_TOKEN = frozenset(".*> \t\r\n")


def publisher(claim: str) -> str:
    """The subject token for a message's claimed publisher: an adapter's journal
    source and log service read `adapter:<name>`, its state and verdicts `<name>`."""
    token = claim.removeprefix("adapter:") or CORE
    if _NOT_IN_TOKEN.intersection(token):
        raise ValueError(f"{claim!r} cannot name a publisher on the bus")
    return token


def publisher_of(subject: str) -> str:
    """The publisher a subject names: its third token (`dida.<kind>.<pub>…`)."""
    parts = subject.split(".", 3)
    return parts[2] if len(parts) > 2 else ""


def vouched(subject: str, claim: str) -> bool:
    try:
        return publisher_of(subject) == publisher(claim)
    except ValueError:
        return False


def state_subject(adapter: str, entity_id: str) -> str:
    return f"{STATE_SUBJECT_PREFIX}.{publisher(adapter)}.{entity_id}"


def entity_subject(adapter: str, entity_id: str) -> str:
    return f"{ENTITY_SUBJECT_PREFIX}.{publisher(adapter)}.{entity_id}"


def reachability_subject(adapter: str) -> str:
    return f"{REACHABILITY_SUBJECT}.{publisher(adapter)}"


def heartbeat_subject(adapter: str) -> str:
    return f"{HEARTBEAT_SUBJECT}.{publisher(adapter)}"


def journal_subject(source: str) -> str:
    return f"{JOURNAL_SUBJECT}.{publisher(source)}"


def logs_subject(service: str) -> str:
    return f"{LOGS_SUBJECT}.{publisher(service)}"


def command_subject(entity_id: str) -> str:
    return f"{COMMAND_SUBJECT_PREFIX}.{entity_id.split(':', 1)[0]}.{entity_id}"


def namespace_commands(namespace: str) -> str:
    return f"{COMMAND_SUBJECT_PREFIX}.{namespace}.>"


class StateUpdate(msgspec.Struct, frozen=True, omit_defaults=True):
    """A device reported a capability value. Published by adapters."""

    entity_id: str
    capability: str
    value: Value
    adapter: str
    ts_ns: int
    unit: str | None = None
    # Optional human-friendly device name (adapter-supplied). The engine stores
    # it on the entity so the UI shows "Backdoor Light 1" instead of a slug id.
    name: str | None = None
    # True for diagnostic/technical entities (wifi signal, uptime, …). The UI
    # keeps these out of the curated view and shows them in a separate section.
    diagnostic: bool = False
    # Display grouping for a device card: "control" (primary, hero/readings) |
    # "config" (settings, collapsed section) | "diagnostic" (technical, collapsed).
    # The adapter classifies each facet; the UI sections by it. Orthogonal to the
    # `diagnostic` flag (which drives the exposed-by-default lean).
    category: str = "control"
    # Optional grouping key for entities that are facets of ONE physical device
    # (e.g. a Marantz exposed as a HEOS player + AVR zones from two adapters, or
    # an ESPHome node's many entities). Adapters that can share a stable identity
    # (typically the device IP) set the same value; the UI groups them into one
    # card. None → ungrouped (rendered standalone, as before).
    device: str | None = None
    # Friendly name of the physical device (the card header). Adapters that group
    # via `device` but never announce EntityInfo set this so the card shows a real
    # name, not a prettified slug. The engine upserts it into `devices` (once,
    # first-write-wins) exactly like EntityInfo.device_name does.
    device_name: str | None = None
    # When the bus first stored this update, on the NATS server's clock; stamped on
    # consume, never by an adapter. Freshness is measured against it: `ts_ns` is
    # the source's clock (a device, another installation), and one running behind
    # made every trigger from it look stale.
    received_ns: int | None = None


class EntityInfo(msgspec.Struct, frozen=True, omit_defaults=True):
    """An adapter announcing that an entity EXISTS, with its catalog metadata —
    independent of any state value. This lets the registry list entities that
    have no state yet (a write-only `press` button) or that the user hasn't
    exposed, so the UI can show a device's full field list and let the user pick
    which to expose. Published by adapters on connect.

    `capabilities` empty means the device offers the field but DIDA can't map its
    type yet — it's shown in the device's list (with `device_type`) but not
    exposable. `device_type` is the adapter's native type hint (e.g. "number").
    """

    entity_id: str
    adapter: str
    capabilities: list[str] = []
    name: str | None = None
    device: str | None = None       # grouping key (device_key), same as StateUpdate.device
    device_name: str | None = None  # friendly name of the physical device (UI card header)
    device_type: str | None = None  # adapter-native type, for display of unmapped fields
    diagnostic: bool = False
    # Display grouping — see StateUpdate.category ("control" | "config" | "diagnostic").
    category: str = "control"
    # The device's identity in ITS protocol, immutable across renames: zigbee2mqtt's
    # ieee_address, Tuya's device id. entity_id is derived from the NAME, so without
    # this a rename looks exactly like a new device — a duplicate appears, the old
    # entity goes stale, and every rule that named it keeps loading and never fires.
    # Optional: adapters whose protocol has no stable id (SSDP/mDNS discovery by
    # friendly name) leave it empty and behave as before.
    native_key: str | None = None
    # The installation the device stands at when that is not this one: a peer DIDA
    # (Cabin), a remote Frigate (Seaside). None is this house.
    site: str | None = None


class Command(msgspec.Struct, frozen=True, omit_defaults=True):
    """Act on a device. Published by the engine, consumed by the owning adapter."""

    entity_id: str
    capability: str
    command: str
    ts_ns: int
    # Command-specific args, e.g. {"value": 80} for set_brightness.
    args: dict[str, Value] = {}
    # Who initiated this command — the audit-trail identity, free-form but
    # conventioned: "user:<username>" (direct UI/REST), "assistant:<username>"
    # (AI chat), "automation:<id>:<name>", "matter" (voice assistants via the
    # bridge). A relay (announce, radio tuner) PROPAGATES the source it received,
    # so the trail keeps the initiator. Empty → Bus.publish_command stamps the
    # publishing client's name as a fallback, so no command is ever anonymous.
    source: str = ""


class JournalEvent(msgspec.Struct, frozen=True, omit_defaults=True):
    """One discrete thing that HAPPENED, as opposed to a value that changed.

    State history answers "what was the temperature at 14:20"; this answers "why
    did the light come on" and "when did this sensor stop reporting". They are
    different questions and different shapes — a measurement has a value, an event
    has a cause — so they are separate streams that the UI merges per device.

    `kind` is a small closed vocabulary (online/offline/command/automation_fired/
    validation_rejected/…) so a timeline can style and filter on it; `data` carries
    the specifics as JSON, because what matters differs per kind and inventing a
    column per case would fossilise today's list.
    """

    ts_ns: int
    kind: str
    # The thing it happened TO. Either may be empty: a device-level event (a bridge
    # reconnecting) has no entity, an ungrouped entity has no device key.
    entity_id: str = ""
    device_key: str = ""
    # Who observed it: engine | automation | adapter:<name> | netmgr | api.
    source: str = ""
    severity: str = "info"   # debug | info | notice | warning | error
    message: str = ""
    data: str = ""           # JSON detail; "" when the kind says it all


class ReachabilityEvent(msgspec.Struct, frozen=True, omit_defaults=True):
    """An adapter's verdict on whether one physical device is reachable right now.

    The adapter is the only thing that KNOWS — it holds the connection, sees the
    availability topic, reads the cloud's online flag. So reachability is asserted
    here, at the source, never inferred downstream from how long a value has gone
    unchanged (that misreads an event-driven switch, quiet for hours, as dead).
    Device-level: every entity grouped under `device_key` inherits the verdict.
    """

    ts_ns: int
    device_key: str
    adapter: str
    reachable: bool
    detail: str = ""   # a short human reason when unreachable ("noise handshake", "MQTT offline")


class LogRecord(msgspec.Struct, frozen=True, omit_defaults=True):
    """One application log line, on its way to durable storage.

    Deliberately NOT a JournalEvent. An event is a fact about the house ("the
    bridge went offline"); a log line is a fact about the software ("reconnect
    attempt 3 of 8"). They have different volumes, different retentions and
    different audiences, and folding them into one shape would force the loud,
    rare thing to share a table with the chatty one.
    """

    ts_ns: int
    service: str
    level: str
    logger: str
    message: str
    entity_id: str = ""   # only when the line is about a specific device
    exc: str = ""         # formatted traceback, when there is one

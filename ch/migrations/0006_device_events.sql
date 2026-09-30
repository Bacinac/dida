-- 0006 — device event journal (PROPOSAL-LOGS-HISTORY §3.2).
--
-- The third firehose. `state_history` records MEASUREMENTS (a value changed);
-- this records THINGS THAT HAPPENED and have no value at all: an adapter went
-- offline, a validation rejected a bad update, an automation fired, a pairing
-- succeeded. Until now those lived for one instant on the bus and were gone —
-- the timeline of a device could show every reading and still not say why it
-- stopped reporting at 03:14.
--
-- `source`: engine | automation | adapter:<name> | netmgr | api
-- `kind`:   online | offline | validation_rejected | automation_fired |
--           automation_error | breaker_trip | reconnect | paired | removed | …
-- `entity_id` is '' for device- or service-level events; `device_key` groups the
-- entities of one physical device (both empty = a whole-service event).
-- `data` is free-form JSON detail (old/new value, automation id, error text).
--
-- severity is a plain LowCardinality(String), not the Enum8 the proposal drafted:
-- an unforeseen value must not make the INSERT fail, because this table is fed by
-- a durable consumer that naks on failure — one bad enum member would nak-loop a
-- message to its dead-letter instead of just recording an odd severity. The
-- journal service validates the set on the way in, where it can complain loudly
-- without wedging delivery.
CREATE TABLE IF NOT EXISTS device_events (
    ts          DateTime64(3, 'UTC'),
    entity_id   LowCardinality(String),
    device_key  LowCardinality(String),
    source      LowCardinality(String),
    kind        LowCardinality(String),
    severity    LowCardinality(String),
    message     String,
    data        String
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (entity_id, ts)
TTL toDateTime(ts) + INTERVAL 180 DAY;

-- Same reason as migration 0005: the journal's durable consumer redelivers on an
-- ambiguous insert failure, and a redelivered event forms a byte-identical
-- single-row block — dropped here instead of becoming a duplicate timeline entry.
ALTER TABLE device_events MODIFY SETTING non_replicated_deduplication_window = 100;

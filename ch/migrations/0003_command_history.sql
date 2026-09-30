-- 0003 — command audit trail.
--
-- Every command on the bus (dida.command.>) is mirrored here by the engine's
-- durable audit consumer: WHO (source) told WHICH entity to do WHAT, when.
-- States tell you what happened; this tells you who asked for it — the forensic
-- other half (couch-light case, 2026-07-11: an unattributed manual turn-on).
--
-- `source` convention: "user:<name>" | "assistant:<name>" | "automation:<id>:<name>"
-- | "matter" | a bus client name as fallback (never empty, stamped at publish).
--
-- No TTL here: retention is data-driven, same mechanism as state_history
-- (retention_class in Postgres → apply_retention() generates MODIFY TTL; commands
-- follow the days_raw tier of the capability they acted on).
CREATE TABLE IF NOT EXISTS command_history (
    ts          DateTime64(3, 'UTC'),
    entity_id   LowCardinality(String),
    capability  LowCardinality(String),
    command     LowCardinality(String),
    source      LowCardinality(String),
    args        String
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (entity_id, ts)

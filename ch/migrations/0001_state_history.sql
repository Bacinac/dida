-- 0001 — baseline state_history firehose.
--
-- Was a lazy `CREATE TABLE IF NOT EXISTS` inline in the engine HistoryWriter;
-- now the ClickHouse schema is versioned here (ch/migrations), applied in order
-- by apply_ch_migrations() at engine boot. Byte-for-byte the table the engine
-- has been writing since Phase 1, so this is a no-op on the existing DB.
--
-- No TTL here: retention is data-driven (retention_class in Postgres → generated
-- ALTER TABLE … MODIFY TTL by apply_retention()). Postgres is the single source.
CREATE TABLE IF NOT EXISTS state_history (
    ts          DateTime64(3, 'UTC'),
    entity_id   LowCardinality(String),
    capability  LowCardinality(String),
    adapter     LowCardinality(String),
    value_num   Nullable(Float64),
    value_str   Nullable(String)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (entity_id, capability, ts)

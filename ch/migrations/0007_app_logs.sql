-- 0007 — application logs, made durable and searchable (PROPOSAL-LOGS-HISTORY §3.3).
--
-- Until now every service logged to stdout only: docker's json-file driver caps
-- each container at ~250 MB and rotates the rest into oblivion. So the answer to
-- "what did the engine say at 03:14 last Tuesday" was, reliably, nothing. stdout
-- stays exactly as it is (`docker logs` is still the live tail); this is the
-- copy that survives, across every service at once, in one place you can query.
--
-- `service`: engine | api | automation | journal | netmgr | adapter:<name>
-- `logger` is the Python logger name (dida.engine.history, dida.bus, …), which
-- is what lets you filter to one subsystem across every service that uses it.
-- `entity_id` is filled only where the log line is about a device — the join
-- that lets a device's timeline show its own log lines next to its events.
--
-- 30 days, deliberately shorter than device_events' 180: logs are high volume and
-- their value decays fast, while an "adapter went offline" event is still worth
-- something next season. `level` is a plain LowCardinality(String) for the same
-- reason device_events' severity is — see 0006.
CREATE TABLE IF NOT EXISTS app_logs (
    ts          DateTime64(3, 'UTC'),
    service     LowCardinality(String),
    level       LowCardinality(String),
    logger      LowCardinality(String),
    entity_id   LowCardinality(String),
    message     String,
    exc         String
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (service, ts)
TTL toDateTime(ts) + INTERVAL 30 DAY;

-- Unlike the journal, logs are BATCHED (they are the firehose of the three), and a
-- retried batch after an ambiguous insert failure would double every line in it.
ALTER TABLE app_logs MODIFY SETTING non_replicated_deduplication_window = 100;

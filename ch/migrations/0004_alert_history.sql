-- 0004 — system alert history.
--
-- Every system-health alert transition the API's evaluator makes (see
-- services/api/src/dida_api/alerts.py) is appended here: WHEN a rule fired or
-- resolved, at what severity, and the measured value that tripped it. The
-- forensic record behind the live active-alerts banner + the Web Push an admin
-- got — "was the NVR adapter really down at 03:14, and for how long".
--
-- `event`: 'fired' | 'resolved'. `scope`: the adapter name for adapter_offline,
-- empty for the global rules (lag/buffer/breaker/disk).
--
-- Low-volume (a few transitions a day), so unlike the state/command firehose this
-- carries an inline 90-day TTL rather than joining the data-driven retention pass.
CREATE TABLE IF NOT EXISTS alert_history (
    ts        DateTime64(3, 'UTC'),
    key       LowCardinality(String),
    scope     LowCardinality(String),
    severity  LowCardinality(String),
    event     LowCardinality(String),
    message   String,
    value     Float64
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (ts)
TTL toDateTime(ts) + INTERVAL 90 DAY

-- 0002 — downsampling rollups for long-range charts + cumulatives.
--
-- Raw state_history keeps ~90d at full resolution (retention_class.days_raw) and,
-- for a home, is cheap to read even for a 90-day window with read-time bucketing.
-- So raw serves EVERY range within its retention; these hourly/daily rollups only
-- serve ranges OLDER than raw retention (year-over-year: "solar production on this
-- day last year"). That is why there is no minute rollup and no backfill — the
-- rollups fill forward and by the time raw expires they already cover the tail.
--
-- AggregatingMergeTree stores -State partials keyed by (entity_id, capability,
-- bucket); the MV writes one partial per inserted block and background merges
-- combine them. Reads finalize with the matching -Merge (avgMerge, argMaxMerge…).
-- No TTL here — apply_retention() sets it per class from Postgres.

CREATE TABLE IF NOT EXISTS state_history_1h (
    bucket      DateTime('UTC'),
    entity_id   LowCardinality(String),
    capability  LowCardinality(String),
    avg_v   AggregateFunction(avg, Float64),
    min_v   AggregateFunction(min, Float64),
    max_v   AggregateFunction(max, Float64),
    first_v AggregateFunction(argMin, Float64, DateTime64(3, 'UTC')),
    last_v  AggregateFunction(argMax, Float64, DateTime64(3, 'UTC')),
    cnt     AggregateFunction(count)
) ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(bucket)
ORDER BY (entity_id, capability, bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS state_history_1h_mv TO state_history_1h AS
SELECT
    toStartOfHour(ts) AS bucket,
    entity_id,
    capability,
    avgState(assumeNotNull(value_num))      AS avg_v,
    minState(assumeNotNull(value_num))      AS min_v,
    maxState(assumeNotNull(value_num))      AS max_v,
    argMinState(assumeNotNull(value_num), ts) AS first_v,
    argMaxState(assumeNotNull(value_num), ts) AS last_v,
    countState()                            AS cnt
FROM state_history
-- Numeric only, deliberately: avg/min/max are meaningless for a text state, and a
-- string capability is LOW-FREQUENCY (a mode or a track title changes a handful of
-- times a day, not once a second), so its raw rows are cheap enough to keep for as
-- long as you want them — the raw tier IS its retention. The Settings → Retention
-- page says so, rather than implying the hourly/daily numbers cover text too.
WHERE value_num IS NOT NULL
GROUP BY entity_id, capability, bucket;

CREATE TABLE IF NOT EXISTS state_history_1d (
    bucket      DateTime('UTC'),
    entity_id   LowCardinality(String),
    capability  LowCardinality(String),
    avg_v   AggregateFunction(avg, Float64),
    min_v   AggregateFunction(min, Float64),
    max_v   AggregateFunction(max, Float64),
    first_v AggregateFunction(argMin, Float64, DateTime64(3, 'UTC')),
    last_v  AggregateFunction(argMax, Float64, DateTime64(3, 'UTC')),
    cnt     AggregateFunction(count)
) ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(bucket)
ORDER BY (entity_id, capability, bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS state_history_1d_mv TO state_history_1d AS
SELECT
    toStartOfDay(ts) AS bucket,
    entity_id,
    capability,
    avgState(assumeNotNull(value_num))      AS avg_v,
    minState(assumeNotNull(value_num))      AS min_v,
    maxState(assumeNotNull(value_num))      AS max_v,
    argMinState(assumeNotNull(value_num), ts) AS first_v,
    argMaxState(assumeNotNull(value_num), ts) AS last_v,
    countState()                            AS cnt
FROM state_history
WHERE value_num IS NOT NULL
GROUP BY entity_id, capability, bucket

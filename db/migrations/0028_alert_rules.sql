-- System-health alert rules — the tunable thresholds the API's alert evaluator
-- checks each cycle (see services/api/src/dida_api/alerts.py). Seeded with sane
-- defaults; an admin edits threshold / hold / enabled from Settings → Upozorenja.
-- These are OPS rules over the platform's own health, deliberately NOT device
-- automations (system health is not a device capability), so they live here, not
-- in `automations`.
CREATE TABLE IF NOT EXISTS alert_rules (
    key        TEXT PRIMARY KEY,             -- stable id; maps to an i18n label + an evaluator condition
    severity   TEXT NOT NULL,                -- 'critical' (Web Push to admins) | 'warning' (banner only)
    threshold  DOUBLE PRECISION,             -- numeric bound; NULL for boolean-presence rules (any offline / any breaker)
    hold_s     INTEGER NOT NULL DEFAULT 60,  -- anti-flap: condition must hold this long before firing
    enabled    BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the five defaults ON CONFLICT DO NOTHING so a re-run (or a user's later
-- tuning) is never clobbered — the seed only fills an empty table.
INSERT INTO alert_rules (key, severity, threshold, hold_s) VALUES
    ('adapter_offline', 'critical', NULL,   60),  -- an adapter stops answering for >60s (transient restarts don't fire)
    ('consumer_lag',    'critical', 1000,   60),  -- JetStream state backlog over 1000 msgs, held 60s (engine wedged / behind)
    ('buffer_drop',     'critical', NULL,    0),  -- history buffer dropped rows (ClickHouse down / buffer full) — edge, fire at once
    ('breaker_open',    'warning',  NULL,    0),  -- one or more automations auto-disabled by their failure breaker
    ('disk_low',        'warning',  15,      0)   -- free space on the state disk (postgres/clickhouse/nats) under 15%
ON CONFLICT (key) DO NOTHING;

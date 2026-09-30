-- Append-only run log for automations: every actual firing (or error) is
-- recorded so the UI can show a history, not just the LAST run (last_triggered_at
-- / last_error on the automations row). Low volume (a handful per minute), so it
-- lives in Postgres next to the automations it describes rather than the
-- ClickHouse state firehose. No FK to automations: a run survives its rule being
-- deleted (it's history), and `name` is denormalised so a deleted rule still
-- reads. The automation service prunes rows older than 30 days.
CREATE TABLE IF NOT EXISTS automation_runs (
    id            BIGSERIAL PRIMARY KEY,
    automation_id BIGINT NOT NULL,
    name          TEXT NOT NULL,
    outcome       TEXT NOT NULL,          -- 'fired' | 'error'
    detail        TEXT,                   -- error message for 'error', else null
    fired_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_fired_at ON automation_runs (fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_runs_aid ON automation_runs (automation_id, fired_at DESC);

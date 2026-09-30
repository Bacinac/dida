-- A light switched on by hand, which no rule may turn off while its room is
-- occupied. The automation engine writes it; the UI shows it on the light.
CREATE TABLE IF NOT EXISTS manual_overrides (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id) ON UPDATE CASCADE ON DELETE CASCADE,
    since     TIMESTAMPTZ NOT NULL DEFAULT now()
);

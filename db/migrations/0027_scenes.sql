-- Scenes: named snapshots of device state you can recall in one tap (the HA-style
-- pillar the SCORECARD flagged as missing). A scene stores a list of settable
-- {entity_id, capability, value} captured from current_state; recall turns each
-- back into a command via dida_core.setter_command and republishes it. Only
-- settable capabilities are captured (sensors / momentary verbs are skipped at
-- capture time), so a recall is always a well-formed set of commands.
CREATE TABLE IF NOT EXISTS scenes (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    states     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{entity_id, capability, value}, ...]
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Case-insensitive unique name so "Movie" and "movie" can't both exist.
CREATE UNIQUE INDEX IF NOT EXISTS scenes_name_key ON scenes (lower(name));

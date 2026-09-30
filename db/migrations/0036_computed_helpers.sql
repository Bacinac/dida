-- Computed helpers — a PURE derivation layer that sits BELOW automations.
--
-- A computed helper is a named value DEFINED by a rule over known statuses, using the
-- automation engine's own language (a Starlark expression that reads state() and
-- assigns `value`). It is NOT an automation: it can run NO action — its only output
-- is its own value. That is the whole point. Because a helper cannot command a device,
-- it can never be part of a feedback loop, so helpers form the layer that is computed
-- FIRST, from raw statuses, and that normal automations then consume in their
-- triggers / conditions / actions — without any risk of the infinite loop you would
-- get if helpers were defined by ordinary (action-running) automations.
--
-- The automation service evaluates them: it watches the state() inputs each script
-- references (auto-extracted), recomputes on a change, and publishes the value as the
-- helper's own entity (`helper:<slug>`), which the engine projects like any state.
CREATE TABLE IF NOT EXISTS computed_helpers (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id   TEXT NOT NULL UNIQUE,          -- helper:<slug>
    name        TEXT NOT NULL,                 -- friendly name (UI + entity label)
    capability  TEXT NOT NULL,                 -- the value TYPE (text/boolean/number/enum/…)
    script      TEXT NOT NULL,                 -- Starlark: reads state(), assigns `value`
    enabled     BOOLEAN NOT NULL DEFAULT true,
    last_error  TEXT,                          -- surfaced in the UI; set on a bad eval
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

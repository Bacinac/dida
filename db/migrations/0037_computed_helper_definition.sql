-- Computed helpers reuse the FULL automation engine instead of a hand-rolled rule
-- language. A helper's rule was a Starlark script; it becomes a DEFINITION built from
-- the SAME Trigger/Condition model automations use, evaluated by the SAME
-- `eval_condition`. The no-code condition editor and the whole typed layer are then
-- reused, not re-coded — and Starlark stays only as an optional "advanced" field.
--
--   definition = {
--     "branches": [ { "conditions": [<Condition>…], "value": <v> }, … ],  -- first match wins
--     "default":  <v>,                                                    -- when no branch matches
--     "script":   ""   -- optional advanced Starlark (run_value); overrides branches when set
--   }
--
-- The table is new (0036) and unused, so the `script` column is simply replaced.
ALTER TABLE computed_helpers DROP COLUMN IF EXISTS script;
ALTER TABLE computed_helpers ADD COLUMN IF NOT EXISTS definition JSONB NOT NULL DEFAULT '{}'::jsonb;

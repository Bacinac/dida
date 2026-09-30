-- Three things so a household's CONFIG (quiet-hours boundaries, thresholds) is
-- defined and edited like any other helper, on the Helpers page — not buried in a
-- Starlark script.
--
-- 1) `category` lets a helper declare itself CONFIG rather than a controllable
--    device. A config helper is a setting, not a thing in a room, so the device /
--    floor-plan views and the automation entity-picker (which already hide
--    non-'control' entities) leave it out. Existing helpers stay 'control'.
ALTER TABLE virtual_entities
    ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'control';

-- 2) `default_value` is the value a helper is SEEDED with the first time it is seen
--    (the virtual adapter can't seed current_state here — that row has a FK to
--    entities, which only exists once the adapter has announced the entity at
--    runtime). Null = the type's zero (false / "" / 0 / 00:00), unchanged for every
--    existing helper.
ALTER TABLE virtual_entities
    ADD COLUMN IF NOT EXISTS default_value JSONB;

-- `time` (settable minutes-since-midnight, defined in capabilities.py) is what a
-- quiet-hours boundary is built from; the boundaries themselves are created on
-- the Helpers page, per installation.

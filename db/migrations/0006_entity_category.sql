-- Display grouping for a device card's facet fields: primary controls vs a
-- collapsed settings block vs diagnostics. The adapter classifies each field
-- (e.g. a TRV's system_mode/preset = control, its calibration/schedules =
-- config, its battery_low/linkquality = diagnostic); the UI sections the card
-- by it instead of dumping every exposed field into one flat list.
--
-- 'control' is the default so every existing entity (and any adapter that never
-- sets a category) keeps its current placement; only facets the adapter marks
-- 'config'/'diagnostic' move into the collapsed sections.
ALTER TABLE entities ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'control';

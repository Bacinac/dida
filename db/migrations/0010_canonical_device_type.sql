-- device_type becomes DIDA's SINGLE canonical device kind — its own source of
-- truth, not "the adapter's live value plus a user override" (0008). Once DIDA
-- knows a thing is a light, it is a light: the adapter's re-announce may refresh
-- name/state but NEVER the type again (see the engine's seed-once upsert).
--
-- The user's explicit type (device_type_override) IS that truth wherever it was
-- set, so fold it into device_type, then drop the override column entirely — no
-- parallel type state remains. Entities whose adapter never sent a type hint keep
-- a NULL here; the engine backfills them from capability classification at boot
-- (and every re-announce seeds any that are still null), so every entity ends up
-- with a concrete type.
UPDATE entities SET device_type = device_type_override WHERE device_type_override IS NOT NULL;
ALTER TABLE entities DROP COLUMN device_type_override;

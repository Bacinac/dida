-- User overrides for an entity's display name and type, kept SEPARATE from the
-- adapter-provided name/device_type. The engine upsert keeps refreshing name and
-- device_type from the adapter on every EntityInfo announce (COALESCE(EXCLUDED,…)),
-- so a user edit written back into those columns would be clobbered on the next
-- sync. These override columns are never in the upsert (like area_id/exposed), so
-- they survive. The UI prefers the override when set; clearing it (NULL) falls
-- back to the adapter's live value. Mirrors the devices name/label split, extended
-- to per-entity (per-gang) granularity — e.g. a multi-relay switch where one gang
-- drives a light gets its own name + type.
ALTER TABLE entities ADD COLUMN IF NOT EXISTS label TEXT;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS device_type_override TEXT;

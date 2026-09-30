-- DIDA Postgres schema — zones (presence geofence regions).
--
-- Added 2026-06-29. Like areas, a zone is a pure config row in the system of
-- record (NOT a bus entity): a named geographic region with a centre + radius,
-- mirroring Home Assistant zones ("Home", "Island house", "Coast house", …).
-- Consumed by the dashboard and by automations; later by person / device-tracker
-- presence ("who is at <zone>"). The one flagged `is_home` is the primary home
-- (HA's special Home zone) — astro/presence default.

CREATE TABLE IF NOT EXISTS zones (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL,
    latitude    DOUBLE PRECISION NOT NULL,    -- decimal degrees
    longitude   DOUBLE PRECISION NOT NULL,    -- decimal degrees
    radius_m    REAL NOT NULL DEFAULT 100,    -- geofence radius in metres
    is_home     BOOLEAN NOT NULL DEFAULT false,  -- the primary home zone
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one home zone.
CREATE UNIQUE INDEX IF NOT EXISTS idx_zones_one_home ON zones (is_home) WHERE is_home;

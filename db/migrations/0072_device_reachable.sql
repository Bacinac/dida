-- Whether a device is reachable right now, as its adapter last reported. Separate
-- from last_seen: a device can hold a fresh reading and be unreachable (Bara's
-- ceiling lights read "on" for hours after they lost power). Default true so every
-- existing device starts reachable and an adapter that never reports reachability
-- leaves its devices looking normal, not falsely dead.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS reachable BOOLEAN NOT NULL DEFAULT true;
-- When the current reachable value was set — drives "unreachable for 20 min" on the
-- floor plan and the alert's hold, and is NOT last_seen (which any state write bumps).
ALTER TABLE devices ADD COLUMN IF NOT EXISTS reachable_since TIMESTAMPTZ NOT NULL DEFAULT now();

-- Whether each adapter namespace is still heard from, as the engine judges it by
-- heartbeats. Kept apart from devices.reachable: that column is the adapter's own
-- verdict on a device, and an adapter that has died cannot revise it. A device is
-- effectively reachable only when both say so.
CREATE TABLE IF NOT EXISTS adapter_liveness (
    adapter TEXT PRIMARY KEY,
    alive BOOLEAN NOT NULL,
    since TIMESTAMPTZ NOT NULL DEFAULT now()
);

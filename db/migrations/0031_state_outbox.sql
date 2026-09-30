-- Transactional outbox for the engine's republished state events.
--
-- The engine commits a validated update to current_state and then republishes it on
-- the events bus for the api/automations. Those were two separate acts, so a crash
-- (or three failed publishes) between them lost the event PERMANENTLY and SILENTLY:
-- the JetStream redelivery that should have saved it is deduped away by the ts_ns
-- guard in _UPSERT_STATE (the row is already stored → `applied` is false → the engine
-- correctly skips it), leaving current_state holding a value that automations and the
-- UI never heard about.
--
-- Recording the intent to publish IN THE SAME TRANSACTION as the projection makes the
-- two atomic: either both survive the commit or neither does. A relay then drains the
-- rows onto the bus and deletes them once NATS has flushed. Written by the engine's
-- single, sequential state consumer, so `id` order is publish order.
CREATE TABLE IF NOT EXISTS state_outbox (
    id         BIGSERIAL PRIMARY KEY,
    payload    BYTEA       NOT NULL,  -- msgspec-encoded StateUpdate, as publish_event sends it
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

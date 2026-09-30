-- A device's identity, independent of what it is called.
--
-- entity_id is derived from the device's NAME (slugged), so renaming a device in
-- zigbee2mqtt or the Tuya app changes its id. Today that produces a duplicate: a
-- new entity appears under the new slug, the old one goes stale and stops
-- updating, and every automation, scene, area and schedule still names the OLD id.
-- Nothing fails — the rules keep loading and simply never fire again. On this
-- installation one such rename would silently kill ten rules (measured against
-- production: helper:daynight is referenced by nine light automations and the
-- vacuum).
--
-- The fix is not to re-key everything on a stable id — that would rewrite 996
-- entity ids and 155 references to solve a problem that only happens on rename.
-- It is to RECOGNISE the rename: the adapter reports the device's native, immutable
-- key (zigbee2mqtt's ieee_address, Tuya's device id — verified present for all 30
-- zigbee devices here), and a device reappearing under a new slug with a key we
-- already know is a rename, not a new device.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS native_key TEXT;

-- One physical device per (adapter, native key). Partial, because adapters that
-- have no stable native id keep reporting NULL and must not collide with each
-- other. Not UNIQUE across adapters: two protocols can legitimately use the same
-- numbering.
CREATE UNIQUE INDEX IF NOT EXISTS devices_adapter_native_key
    ON devices (adapter, native_key) WHERE native_key IS NOT NULL;

-- Renaming means UPDATEing entities.entity_id, and current_state references it.
-- The original constraint cascades DELETE but not UPDATE, so the update would be
-- rejected by the foreign key with its children still pointing at the old id —
-- the rename would fail at the last step, after the references had been rewritten.
-- Recreating the constraint with ON UPDATE CASCADE moves the live state with the
-- entity, in the same transaction. Nothing is dropped: the constraint is replaced
-- by an equivalent one that also handles the update case.
ALTER TABLE current_state DROP CONSTRAINT IF EXISTS current_state_entity_id_fkey;
ALTER TABLE current_state ADD CONSTRAINT current_state_entity_id_fkey
    FOREIGN KEY (entity_id) REFERENCES entities(entity_id)
    ON DELETE CASCADE ON UPDATE CASCADE;

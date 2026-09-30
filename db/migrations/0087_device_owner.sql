-- A device's owner is the adapter that registered it, and the engine no longer
-- hands the row to whichever adapter last grouped an entity into it (heos under
-- the AVR the denon adapter registered). Freeze each row on the namespace most of
-- its entities carry, so ownership does not stay with whoever wrote last before
-- this release.
UPDATE devices d SET adapter = x.ns
FROM (
    SELECT device_key, mode() WITHIN GROUP (ORDER BY split_part(entity_id, ':', 1)) AS ns
    FROM entities WHERE device_key IS NOT NULL GROUP BY device_key
) x
WHERE x.device_key = d.device_key AND d.adapter <> x.ns;

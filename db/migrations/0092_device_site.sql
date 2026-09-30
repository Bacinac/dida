-- The installation a device stands at when it is not this house: a peer DIDA, a
-- remote Frigate. NULL is this house. The adapter announces it; the Devices page
-- groups by it.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS site TEXT;

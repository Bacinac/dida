-- 0012 — the floor's editable BORDER layer + floor-switch marker position.
--
-- extract_borders (planvision) seeds a draft from the plan's drawn walls:
-- [[x, y, w, h], …] rectangles in % of the plan. The user shapes that draft in the UI
-- (drag / resize / add / delete / merge-erase) and saves the confirmed set here. Rooms
-- are derived from THESE borders, never from the raw image — and not every border is a
-- wall: a patio or garden edge bounds an outdoor area the same way.
ALTER TABLE floors ADD COLUMN IF NOT EXISTS borders JSONB;

-- Floor-switch marker position on the plan (% coords; NULL → default corner). With >1
-- floor the plan shows a built-in stairs marker that switches floors — per-client UI
-- navigation, deliberately NOT an entity on the bus (two viewers browse independently).
ALTER TABLE floors ADD COLUMN IF NOT EXISTS switch_x REAL;
ALTER TABLE floors ADD COLUMN IF NOT EXISTS switch_y REAL;

-- One HOME-WIDE multiplier for every device/sensor marker on the plan (NULL → 1.0);
-- composes with each marker's own per-device scale. Deliberately the same value on
-- every floors row (like switch_x/y): it lives here so any signed-in user gets it
-- with GET /floors — no extra settings surface.
ALTER TABLE floors ADD COLUMN IF NOT EXISTS marker_scale REAL;

-- A presence/motion sensor is placed on the plan SEPARATELY from its data readings:
-- the motion indicator (invisible until it fires) and the lux/temp value label are two
-- markers the user positions independently. Many sensors carry both on ONE entity (a
-- zigbee Aqara reports occupancy + illuminance together), so the motion marker needs
-- its own placement slot distinct from fp_floor/fp_x/fp_y (which the value label uses).
ALTER TABLE entities ADD COLUMN IF NOT EXISTS fp_motion_floor TEXT;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS fp_motion_x REAL;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS fp_motion_y REAL;

-- A room's sensor readings are shown as ONE aggregated label (mean per capability
-- across the room's sensors), not scattered per-sensor. This holds that label's
-- config: which capabilities it shows, which sensors are excluded from a mean, and
-- which capability is the collapsed primary. { "hidden": ["pressure"], "excluded":
-- ["mqtt:sensorB:temperature"], "primary": "temperature" }.
ALTER TABLE areas ADD COLUMN IF NOT EXISTS sensor_config JSONB;

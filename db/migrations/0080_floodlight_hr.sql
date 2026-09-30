-- 0080_floodlight_hr.sql
-- Croatian for the camera floodlight the BABA adapter announces. The adapter emits
-- the English descriptor (`Floodlight`) like every other one; this is display only.
INSERT INTO translations (key, lang, value) VALUES
  ('Floodlight', 'hr', 'Reflektor')
ON CONFLICT (key, lang) DO NOTHING;

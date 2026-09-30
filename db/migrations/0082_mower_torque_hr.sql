-- 0082_mower_torque_hr.sql
-- Croatian for the mower's torque trim, the one drive setting Worx exposes. The
-- adapter emits the English descriptor; this is display only.
INSERT INTO translations (key, lang, value) VALUES
  ('Wheel torque', 'hr', 'Moment kotača')
ON CONFLICT (key, lang) DO NOTHING;

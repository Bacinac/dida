-- 0081_light_condition_hr.sql
-- Croatian for the camera light bands BABA measures and the baba adapter mirrors
-- (`light_condition`: ir | dark | dim | normal | bright). Display only — the value
-- on the wire stays the English token an automation refers to.
--
-- Keyed on the English word, so a token already translated for another vocabulary
-- keeps the one word it has; ON CONFLICT leaves those untouched.
INSERT INTO translations (key, lang, value) VALUES
  ('ir', 'hr', 'infracrveno'),
  ('dark', 'hr', 'mrak'),
  ('dim', 'hr', 'prigušeno'),
  ('normal', 'hr', 'normalno'),
  ('bright', 'hr', 'svijetlo')
ON CONFLICT (key, lang) DO NOTHING;

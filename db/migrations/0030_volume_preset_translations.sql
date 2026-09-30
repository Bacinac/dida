-- 0030_volume_preset_translations.sql
-- Croatian for the volume-preset LEVEL names. Presets are authored in ENGLISH (the
-- source language, like every adapter descriptor); the UI localises them via the
-- translations dictionary (Settings → Prijevodi / autofill), so an EN session shows
-- the base and an HR session shows these. DO NOTHING preserves any curated override.
INSERT INTO translations (key, lang, value) VALUES
  ('Quiet',  'hr', 'Tiho'),
  ('Normal', 'hr', 'Normalno'),
  ('Louder', 'hr', 'Glasnije'),
  ('Loud',   'hr', 'Glasno'),
  ('Party',  'hr', 'Party')
ON CONFLICT (key, lang) DO NOTHING;

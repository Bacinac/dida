-- 0077_vacuum_vocabulary_hr.sql
-- Croatian for the closed vocabularies a picker offers. Adapters emit English
-- tokens (the value on the wire is the identity); this dictionary is display only,
-- so translating one never changes what a rule or a command refers to.
--
-- Keyed on the English word, so a token shared by two adapters gets one Croatian
-- word — deliberately: `silent` is a suction level here and a fan mode on the air
-- conditioner, and "tiho" is right in both.
INSERT INTO translations (key, lang, value) VALUES
  -- what the robot does to the floor (vacuum_mode)
  ('sweeping', 'hr', 'usisavanje'),
  ('mopping', 'hr', 'pranje'),
  ('sweeping and mopping', 'hr', 'usisavanje i pranje'),
  ('mopping after sweeping', 'hr', 'pranje nakon usisavanja'),
  -- suction level (vacuum + air conditioner fan modes)
  ('silent', 'hr', 'tiho'),
  ('basic', 'hr', 'osnovno'),
  ('strong', 'hr', 'jako'),
  ('max', 'hr', 'maksimalno'),
  -- how wet the mop runs, and fan steps that share the words
  ('low', 'hr', 'nisko'),
  ('medium', 'hr', 'srednje'),
  ('high', 'hr', 'visoko'),
  -- what the station is doing
  ('idle', 'hr', 'mirovanje'),
  ('paused', 'hr', 'pauzirano'),
  ('returning', 'hr', 'povratak'),
  ('washing', 'hr', 'pranje krpa'),
  ('drying', 'hr', 'sušenje'),
  ('adding water', 'hr', 'dolijevanje vode'),
  ('returning to dry', 'hr', 'povratak na sušenje'),
  -- station stock and wear parts
  ('ok', 'hr', 'uredno'),
  ('missing', 'hr', 'nedostaje'),
  ('full', 'hr', 'pun'),
  ('check', 'hr', 'provjeriti'),
  ('disabled', 'hr', 'isključeno')
ON CONFLICT (key, lang) DO NOTHING;

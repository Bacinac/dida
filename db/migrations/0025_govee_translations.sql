-- 0025_govee_translations.sql
-- Croatian for the new govee adapter config strings + the repointed ha adapter
-- help (English at source; the UI localises via the translations dictionary).
INSERT INTO translations (key, lang, value) VALUES
  ('Govee email', 'hr', 'Govee e-mail'),
  ('Govee password', 'hr', 'Govee lozinka'),
  ('API key (optional)', 'hr', 'API ključ (opcionalno)'),
  ('Govee account email. Uses the account (AWS IoT) path — realtime updates, no LAN needed.', 'hr',
   'E-mail Govee računa. Koristi account (AWS IoT) put — realtime, bez LAN-a.'),
  ('Govee account password. Stored encrypted; written to the bridge, never shown.', 'hr',
   'Lozinka Govee računa. Sprema se kriptirano; upisuje se u bridge, nikad se ne prikazuje.'),
  ('Govee HTTP API key (Govee Home app → profile → Apply for API Key). Only needed for scene control on devices without LAN.', 'hr',
   'Govee HTTP API ključ (Govee Home app → profil → Apply for API Key). Treba samo za scene na uređajima bez LAN-a.'),
  ('The DIDA MQTT broker where co-located bridges publish HA discovery.', 'hr',
   'DIDA MQTT broker na koji co-located bridgevi objavljuju HA discovery.')
ON CONFLICT (key, lang) DO NOTHING;

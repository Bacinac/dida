-- The device timeline shows a journal event's kind through the translation
-- dictionary, falling back to the raw key. The MQTT adapter now records Zigbee
-- onboarding on the device's own timeline — join, interview outcome and leave —
-- so pairing progress is visible in DIDA instead of only in the z2m console.
INSERT INTO translations (key, lang, value) VALUES
  ('pairing', 'hr', 'uparivanje'),
  ('detail.event.pairing', 'hr', 'uparivanje'),
  ('pairing_failed', 'hr', 'uparivanje nije uspjelo'),
  ('detail.event.pairing_failed', 'hr', 'uparivanje nije uspjelo'),
  ('device_left', 'hr', 'uređaj napustio mrežu'),
  ('detail.event.device_left', 'hr', 'uređaj napustio mrežu')
ON CONFLICT (key, lang) DO NOTHING;

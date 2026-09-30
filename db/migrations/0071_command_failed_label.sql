-- The device timeline shows a journal event's kind through the translation
-- dictionary, falling back to the raw key. `command_failed` is new: the MQTT
-- adapter now records a command zigbee2mqtt could not deliver, which until today
-- was visible only inside zigbee2mqtt's own container log.
INSERT INTO translations (key, lang, value) VALUES
  ('command_failed', 'hr', 'naredba nije izvršena'),
  ('detail.event.command_failed', 'hr', 'naredba nije izvršena'),
  ('device did not respond', 'hr', 'uređaj se nije javio'),
  ('no route to the device', 'hr', 'nema puta do uređaja'),
  ('the radio channel was busy', 'hr', 'radijski kanal je bio zauzet'),
  ('the device did not wake in time', 'hr', 'uređaj se nije probudio na vrijeme'),
  ('timed out', 'hr', 'isteklo vrijeme')
ON CONFLICT (key, lang) DO NOTHING;

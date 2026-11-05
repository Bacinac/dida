-- Croatian for the reason the MQTT adapter gives when the coordinator itself did not answer.
INSERT INTO translations (key, lang, value) VALUES
  ('the coordinator did not answer', 'hr', 'koordinator se nije javio')
ON CONFLICT (key, lang) DO NOTHING;

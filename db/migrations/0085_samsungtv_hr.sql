-- Croatian for the Samsung TV adapter's settings. The schema declares English;
-- this is display only.
INSERT INTO translations (key, lang, value) VALUES
  ('TV — IP', 'hr', 'Televizor — IP'),
  ('Samsung TV on the LAN. The first time it is on, accept DIDA on the TV screen.', 'hr',
   'Samsung televizor u lokalnoj mreži. Kad se prvi put upali, na ekranu televizora dopustite DIDA-i pristup.'),
  ('Display name; builds the samsungtv:<name> entity.', 'hr', 'Prikazni naziv; gradi entitet samsungtv:<naziv>.')
ON CONFLICT (key, lang) DO NOTHING;

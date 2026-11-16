-- Croatian for the Ponistra sign-in shared by the contacts and calendar adapters.
INSERT INTO translations (key, lang, value) VALUES
  ('Address book', 'hr', 'Adresar'),
  ('Ponistra address', 'hr', 'Adresa Ponistre'),
  ('E-mail', 'hr', 'E-pošta'),
  ('App password', 'hr', 'Lozinka aplikacije'),
  ('Ponistra → Settings → App passwords. Not the account password.', 'hr',
   'Ponistra → Postavke → Lozinke aplikacija. Nije lozinka računa.'),
  ('Away calendar', 'hr', 'Kalendar odsutnosti'),
  ('The calendar''s name in Ponistra. An event in it covering the moment means the household is away. Empty = off.', 'hr',
   'Ime kalendara u Ponistri. Događaj u njemu koji traje u tom trenutku znači da je kućanstvo odsutno. Prazno = isključeno.')
ON CONFLICT (key, lang) DO NOTHING;

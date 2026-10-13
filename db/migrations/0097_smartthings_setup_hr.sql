-- Croatian for the reworded OAuth settings and messages; the old keys go with them.
DELETE FROM translations WHERE key IN (
  'From the SmartThings OAuth app (`smartthings apps:create` → OAuth-In App).',
  'Empty = auto from the public URL (Cloudflare tunnel). This is the value you register in the OAuth app.',
  'Empty = auto from the public URL (Cloudflare tunnel). This is the value you register as an Authorised redirect URI.'
);
INSERT INTO translations (key, lang, value) VALUES
  ('From the OAuth app you create once at SmartThings; the setup guide is linked on this card.', 'hr',
   'Iz OAuth aplikacije koju jednom izradiš na SmartThingsu; upute za postavljanje su poveznica na ovoj kartici.'),
  ('Empty = the public address (Settings → Network) + /api/smartthings/callback. SmartThings accepts only https or http://localhost; register the same value in the OAuth app.', 'hr',
   'Prazno = javna adresa (Postavke → Mreža) + /api/smartthings/callback. SmartThings prihvaća samo https ili http://localhost; istu vrijednost upiši u OAuth aplikaciju.'),
  ('Empty = the public address (Settings → Network) + /api/contacts/callback. This is the value you register as an Authorised redirect URI.', 'hr',
   'Prazno = javna adresa (Postavke → Mreža) + /api/contacts/callback. Tu vrijednost upiši kao Authorised redirect URI.'),
  ('Enter Client ID, Client Secret and Redirect URI, then save.', 'hr',
   'Unesi Client ID, Client Secret i Redirect URI, pa spremi.'),
  ('Enter Client ID and Client Secret, then save.', 'hr',
   'Unesi Client ID i Client Secret, pa spremi.'),
  ('The public address is not externally reachable — set it to the tunnel origin in Settings → Network', 'hr',
   'Javna adresa nije dostupna izvana — u Postavke → Mreža postavi adresu tunela.')
ON CONFLICT (key, lang) DO NOTHING;

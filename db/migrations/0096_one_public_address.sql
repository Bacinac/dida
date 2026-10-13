-- app_url and public_url both held the installation's public address, and only
-- app_url can be changed (Settings → Network). Where public_url names an outside
-- origin and app_url is still loopback, the outside origin is the one to keep.
UPDATE app_settings a
   SET value = p.value, updated_at = now()
  FROM app_settings p
 WHERE a.key = 'app_url' AND p.key = 'public_url'
   AND p.value !~* '^https?://(localhost|127\.|\[::1\])'
   AND p.value <> ''
   AND (a.value = '' OR a.value ~* '^https?://(localhost|127\.|\[::1\])');
INSERT INTO app_settings (key, value)
SELECT 'app_url', value FROM app_settings WHERE key = 'public_url'
ON CONFLICT (key) DO NOTHING;
DELETE FROM app_settings WHERE key = 'public_url';

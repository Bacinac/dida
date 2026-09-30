-- The wall panel's photographs are OPUS's. Immich is retired: its address and
-- key go, and so do the album and the people the slideshow was narrowed to,
-- which named things only Immich knew.
DELETE FROM app_settings WHERE key IN ('immich_url', 'immich_api_key');

UPDATE app_settings
SET value = ((value::jsonb) - 'album_id' - 'person_ids')::text
WHERE key = 'panel_config'
  AND CASE WHEN pg_input_is_valid(value, 'jsonb')
           THEN jsonb_typeof(value::jsonb) = 'object' ELSE false END;

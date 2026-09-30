-- The house's music is OPUS's. The catalogue, the stations and the streaming
-- accounts DIDA kept beside it are gone: what there is to listen to is asked of
-- OPUS · Player on the spot, and the devices fetch the bytes from it.
--
-- What remains of the old media base URL is the one thing it was also used
-- for: the host's own LAN address, which the AV adapters bind discovery to. It
-- is kept under the name of what it is.
DROP TABLE IF EXISTS media_tracks;
DROP TABLE IF EXISTS media_artists;
DROP TABLE IF EXISTS radio_stations;
DROP TABLE IF EXISTS user_service_accounts;

INSERT INTO app_settings (key, value)
SELECT 'lan_ip', regexp_replace(value, '^https?://([^/:]+).*$', '\1')
FROM app_settings
WHERE key = 'media_base_url'
ON CONFLICT (key) DO NOTHING;

DELETE FROM app_settings
WHERE key IN ('media_base_url', 'spotify_client_id', 'spotify_client_secret',
              'jellyfin_url', 'jellyfin_api_key');

-- A space's browse tabs: the local library became the OPUS shelf, and Tidal is
-- no longer something this house streams.
UPDATE areas
SET media_config = jsonb_set(media_config, '{sources}', (
    SELECT COALESCE(jsonb_agg(
        CASE WHEN s ? 'browse' THEN jsonb_set(s, '{browse}', COALESCE((
            SELECT jsonb_agg(to_jsonb(CASE WHEN b = 'local' THEN 'opus' ELSE b END))
            FROM jsonb_array_elements_text(s -> 'browse') AS b
            WHERE b <> 'tidal'
        ), '[]'::jsonb))
        ELSE s END
    ), '[]'::jsonb)
    FROM jsonb_array_elements(media_config -> 'sources') AS s
))
WHERE media_config IS NOT NULL
  AND jsonb_typeof(media_config -> 'sources') = 'array'
  AND jsonb_array_length(media_config -> 'sources') > 0;

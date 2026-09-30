-- 0003 — consolidate all the DDL that services used to self-heal in code.
--
-- Before this, each service CREATE-TABLE-IF-NOT-EXISTS'd its own tables at boot
-- (engine _MIGRATE, api adapter_config/radio/tokens/pipeline SCHEMA, medialib
-- _SCHEMA, netmgr/lanprobe app_settings). That scattered the schema across ~8
-- sites and let a fresh initdb-only DB be incomplete until every service booted.
-- Now db/migrations is the single source and a runner applies it; this file is
-- byte-for-byte the union of that former in-code DDL. Everything is idempotent,
-- so it is a no-op on an already-self-healed database.

-- engine: entities/areas floor-plan + exposure columns, device registry
ALTER TABLE entities ADD COLUMN IF NOT EXISTS exposed BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS hidden_caps JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS fp_glow JSONB;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS fp_style JSONB;
ALTER TABLE areas ADD COLUMN IF NOT EXISTS fp_poly JSONB;

CREATE TABLE IF NOT EXISTS devices (
  device_key TEXT PRIMARY KEY, adapter TEXT NOT NULL, name TEXT, label TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen TIMESTAMPTZ NOT NULL DEFAULT now());

-- User-removed devices: the engine drops any update for a key here so a still-
-- polling adapter can't re-create a device the user deleted.
CREATE TABLE IF NOT EXISTS removed_devices (
  key TEXT PRIMARY KEY, adapter TEXT, removed_at TIMESTAMPTZ NOT NULL DEFAULT now());

-- Legacy upgrade: migrate any friendly name kept on entities.device_name into the
-- devices table, then drop that (now-redundant) column. No-op on fresh installs.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name='entities' AND column_name='device_name') THEN
    INSERT INTO devices (device_key, adapter, name)
      SELECT device_key, min(adapter), max(device_name) FROM entities
      WHERE device_key IS NOT NULL AND device_name IS NOT NULL GROUP BY device_key
      ON CONFLICT (device_key) DO NOTHING;
    ALTER TABLE entities DROP COLUMN device_name;
  END IF;
END $$;

-- core.adapter_config: UI-editable per-adapter config (secrets Fernet-encrypted)
CREATE TABLE IF NOT EXISTS adapter_config (
  adapter TEXT NOT NULL,
  key     TEXT NOT NULL,
  value   TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (adapter, key)
);

-- netmgr/lanprobe: generic key/value app settings (vlan_config, lan_status, …)
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- api.pipeline: per-renderer measured-output metadata (signal-path viz)
CREATE TABLE IF NOT EXISTS media_renderer_output (
    entity_id TEXT PRIMARY KEY,
    probe_url TEXT,
    dac       TEXT,
    link      TEXT
);

-- api.radio: curated radio stations
CREATE TABLE IF NOT EXISTS radio_stations (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       TEXT NOT NULL,
    genre      TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL,
    logo       TEXT,
    country    TEXT NOT NULL DEFAULT '',
    sort_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE radio_stations ADD COLUMN IF NOT EXISTS country TEXT NOT NULL DEFAULT '';

-- api.tokens: per-user external music service accounts (Spotify/Tidal OAuth)
CREATE TABLE IF NOT EXISTS user_service_accounts (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service       TEXT NOT NULL,
    account_id    TEXT,
    account_name  TEXT,
    refresh_token TEXT NOT NULL,
    access_token  TEXT,
    expires_at    TIMESTAMPTZ,
    scope         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, service)
);

-- medialib: FLAC library index + per-artist metadata
CREATE TABLE IF NOT EXISTS media_tracks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    title TEXT, artist TEXT, album TEXT, album_artist TEXT,
    track_no INT, disc_no INT, year INT, genre TEXT, duration INT,
    has_art BOOLEAN NOT NULL DEFAULT false,
    sample_rate INT, bits INT, channels INT, bitrate INT,
    mtime DOUBLE PRECISION,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE media_tracks ADD COLUMN IF NOT EXISTS sample_rate INT;
ALTER TABLE media_tracks ADD COLUMN IF NOT EXISTS bits INT;
ALTER TABLE media_tracks ADD COLUMN IF NOT EXISTS channels INT;
ALTER TABLE media_tracks ADD COLUMN IF NOT EXISTS bitrate INT;
CREATE INDEX IF NOT EXISTS idx_media_tracks_album ON media_tracks (album_artist, album, disc_no, track_no);
CREATE INDEX IF NOT EXISTS idx_media_tracks_artist ON media_tracks (lower(artist));
CREATE INDEX IF NOT EXISTS idx_media_tracks_title ON media_tracks (lower(title));

CREATE TABLE IF NOT EXISTS media_artists (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    album_artist  TEXT NOT NULL UNIQUE,
    folder        TEXT,
    name          TEXT,
    sort_name     TEXT,
    mbid          TEXT,
    type          TEXT,
    disambiguation TEXT,
    biography     TEXT,
    country       TEXT,
    formed        TEXT,
    disbanded     TEXT,
    genres        JSONB,
    has_image     BOOLEAN NOT NULL DEFAULT false,
    image_rel     TEXT,
    nfo_mtime     DOUBLE PRECISION,
    indexed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

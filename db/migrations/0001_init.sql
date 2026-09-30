-- DIDA Postgres schema (system of record).
--
-- Phase 0 mounts this into postgres' /docker-entrypoint-initdb.d, so it runs
-- once on first boot of an empty data dir. A real migration runner replaces
-- that in Phase 1 (mirrors BABA's api-driven migrations).
--
-- ClickHouse (history / event firehose) has its own DDL, added in Phase 1.

-- Areas (rooms / zones). Pure UI/grouping concept; entities reference one.
CREATE TABLE IF NOT EXISTS areas (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT,            -- optional user name; NULL → show translated `kind`
    kind        TEXT,            -- canonical room type (bedroom/bathroom/…), translated in UI
    fp_floor    TEXT,            -- floorplan placement: floor key
    fp_x        REAL,            -- floorplan placement: x (0..100 %)
    fp_y        REAL             -- floorplan placement: y (0..100 %)
);

-- Entity registry. One row per logical device endpoint an adapter exposes.
-- `capabilities` is the set of canonical capability kinds this entity offers
-- (e.g. ["on_off","brightness"]). The engine validates every state update
-- against the capability spec before it lands in current_state.
CREATE TABLE IF NOT EXISTS entities (
    entity_id    TEXT PRIMARY KEY,            -- stable id, e.g. "mqtt:kitchen_light"
    name         TEXT,                        -- friendly name (UI)
    adapter      TEXT NOT NULL,               -- which adapter owns it ("mqtt", ...)
    device_type  TEXT,                        -- adapter's native type hint
    area_id      BIGINT REFERENCES areas(id) ON DELETE SET NULL,
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    diagnostic   BOOLEAN NOT NULL DEFAULT false,  -- technical entity → shown separately
    exposed      BOOLEAN NOT NULL DEFAULT true,   -- user chose to expose it (false → hidden from Devices)
    hidden_caps  JSONB NOT NULL DEFAULT '[]'::jsonb,  -- individual capabilities the user hid from Devices
    device_key   TEXT,                        -- groups entities of ONE physical device (UI = one card)
    fp_floor     TEXT,                        -- floorplan placement: floor key
    fp_x         REAL,                        -- floorplan placement: x (0..100 %)
    fp_y         REAL,                        -- floorplan placement: y (0..100 %)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per physical device (a device_key). The friendly NAME lives here ONCE,
-- not smeared across its entities: `name` is auto-detected by the adapter (set on
-- first sight, then sticky), `label` is the user's override. Entities keep their
-- own (protocol-exposed) names; the UI shows label|name as the card header/prefix.
CREATE TABLE IF NOT EXISTS devices (
    device_key TEXT PRIMARY KEY,
    adapter    TEXT NOT NULL,
    name       TEXT,   -- adapter-detected friendly name (device_info)
    label      TEXT,   -- user override
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Current state projection: latest validated value per (entity, capability).
-- The engine upserts here on every accepted update; the api reads it for
-- snapshots and streams live deltas off the bus.
CREATE TABLE IF NOT EXISTS current_state (
    entity_id   TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    capability  TEXT NOT NULL,
    value       JSONB NOT NULL,
    unit        TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, capability)
);

-- Automations. Stored as data (typed Trigger→Condition→Action graph, or a
-- Starlark snippet), versioned, never as YAML on disk. Engine reads, never
-- writes here. `enabled` + a circuit-breaker (Phase 1) gate execution.
CREATE TABLE IF NOT EXISTS automations (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    definition      JSONB NOT NULL,
    last_triggered_at TIMESTAMPTZ,   -- observability: last time the rule fired
    last_error      TEXT,            -- last action error; set when the breaker trips
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Users (argon2 hash + role). Mirrors BABA's scheme. Auth = argon2 + signed
-- JWT in an HttpOnly cookie; `token_version` is the per-user revocation counter
-- (bumped on password change) so old sessions die without a server-side store.
CREATE TABLE IF NOT EXISTS users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',
    token_version INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

-- App settings (admin-managed secrets + config). Holds e.g. the assistant's
-- Anthropic / OpenAI API keys, set from the UI so they can be rotated without a
-- redeploy. Secrets live here (system of record), never returned by the API.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Virtual entities ("helpers" + device-independent state, the DIDA-native
-- replacement for HA input_boolean/input_select/input_number). The `virtual`
-- adapter owns these: it seeds initial state and turns commands into validated
-- state updates, so a virtual entity is first-class on the bus like any device.
-- `capability` is one of boolean | enum | number; `options` carries the enum's
-- allowed values. State itself lives in current_state (single source of truth),
-- never here — this table is only the definition.
CREATE TABLE IF NOT EXISTS virtual_entities (
    entity_id   TEXT PRIMARY KEY,            -- e.g. "virtual:guest_mode"
    name        TEXT,                        -- friendly name (UI)
    capability  TEXT NOT NULL,               -- 'boolean' | 'enum' | 'number'
    options     JSONB,                       -- enum: ["Off","L1","L2","L3"]
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Local music library (DIDA's native media library — the replacement for an
-- external media server). The `medialib` service indexes a mounted music tree
-- (FLAC tags via mutagen) into this table and serves the files + art over HTTP
-- to renderers. One row per track; albums/artists are derived by GROUP BY.
CREATE TABLE IF NOT EXISTS media_tracks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,              -- path relative to the music root
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    album_artist TEXT,
    track_no     INT,
    disc_no      INT,
    year         INT,
    genre        TEXT,
    duration     INT,                               -- seconds
    has_art      BOOLEAN NOT NULL DEFAULT false,
    sample_rate  INT,                               -- Hz (e.g. 44100, 96000)
    bits         INT,                               -- bit depth (16 / 24)
    channels     INT,
    bitrate      INT,                               -- bits per second
    mtime        DOUBLE PRECISION,                  -- source mtime for incremental reindex
    indexed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-artist metadata harvested from the collection's own artist.nfo / artist.jpg.
CREATE TABLE IF NOT EXISTS media_artists (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    album_artist   TEXT NOT NULL UNIQUE,
    folder         TEXT,                            -- top-level artist dir under the music root
    name           TEXT,
    sort_name      TEXT,
    mbid           TEXT,                            -- MusicBrainz artist id
    type           TEXT,                            -- Group / Person …
    disambiguation TEXT,
    biography      TEXT,
    country        TEXT,
    formed         TEXT,
    disbanded      TEXT,
    genres         JSONB,
    has_image      BOOLEAN NOT NULL DEFAULT false,
    image_rel      TEXT,                            -- "<folder>/artist.jpg"
    nfo_mtime      DOUBLE PRECISION,
    indexed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-user OAuth tokens for external music services (Spotify / Tidal). Tokens
-- are stored Fernet-encrypted by the API (see dida_api/tokens.py).
CREATE TABLE IF NOT EXISTS user_service_accounts (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service       TEXT NOT NULL,            -- 'spotify' | 'tidal'
    account_id    TEXT,
    account_name  TEXT,
    refresh_token TEXT NOT NULL,            -- encrypted
    access_token  TEXT,                     -- encrypted (cached)
    expires_at    TIMESTAMPTZ,
    scope         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, service)
);

-- Per-renderer output config for the audio signal-path view: the renderer's own
-- output-state API (so DIDA can read what it's really decoding) and, optionally,
-- a DIGITAL DAC it feeds over USB/SPDIF (whose input format is therefore
-- measurable). The analog tail past the DAC is NOT modelled — DIDA can't measure it.
CREATE TABLE IF NOT EXISTS media_renderer_output (
    entity_id TEXT PRIMARY KEY,
    probe_url TEXT,   -- renderer's own output-state API (e.g. Volumio getState)
    dac       TEXT,   -- a digital DAC fed by the renderer
    link      TEXT    -- the digital link to that DAC, e.g. 'USB' / 'Coax SPDIF'
);

-- DIDA's own editable radio station list (best direct stream URLs), pushed to
-- any player. Seeded with curated defaults on first API boot (radio.seed_if_empty).
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

CREATE INDEX IF NOT EXISTS idx_entities_adapter ON entities (adapter);
CREATE INDEX IF NOT EXISTS idx_current_state_updated ON current_state (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_tracks_album ON media_tracks (album_artist, album, disc_no, track_no);
CREATE INDEX IF NOT EXISTS idx_media_tracks_artist ON media_tracks (lower(artist));
CREATE INDEX IF NOT EXISTS idx_media_tracks_title ON media_tracks (lower(title));

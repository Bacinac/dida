-- Cloudflare tunnel ingress moves from the config.yml FILE to Postgres as the
-- single source of truth. The adapter GENERATES config.yml from these rows (a
-- projection, exactly like DIDA projects device state) and rolling-restarts the
-- connectors to apply it — so routes are DB-backed (they back up / restore with
-- the rest of DIDA) and the file is a derived artifact, never hand-edited.
--
-- The adapter seeds these tables ONCE from the existing config.yml on first boot
-- (a data migration that needs to parse YAML, so it can't live in SQL); after
-- that the DB is authoritative and generation overwrites the file.

CREATE TABLE IF NOT EXISTS cloudflare_config (
    id               BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),  -- single-row table
    tunnel           TEXT NOT NULL,
    credentials_file TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cloudflare_routes (
    id              SERIAL PRIMARY KEY,
    position        INTEGER NOT NULL,                 -- order within ingress (before the catch-all)
    section         TEXT,                             -- optional group header (# ----- <section> -----)
    hostname        TEXT NOT NULL UNIQUE,
    service         TEXT NOT NULL,
    no_tls_verify   BOOLEAN NOT NULL DEFAULT false,
    disable_chunked BOOLEAN NOT NULL DEFAULT false,
    extra           JSONB,                            -- passthrough for un-modelled keys (path, httpHostHeader, …)
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cloudflare_routes_position_idx ON cloudflare_routes (position);

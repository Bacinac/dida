-- 0004 — data-retention policy (UI-editable) for the ClickHouse history tiers.
--
-- Postgres is the single source of truth for how long each resolution tier (raw
-- / hourly / daily) is kept, per capability CLASS. apply_retention() reads this
-- and generates the ClickHouse TTLs (ALTER TABLE … MODIFY TTL). ClickHouse only
-- executes. Editing a number in Settings → Retencija re-applies it live — no
-- migration, no rebuild. `days = 0` drops that tier immediately (not kept).

CREATE TABLE IF NOT EXISTS retention_class (
    name       TEXT PRIMARY KEY,
    label      TEXT NOT NULL DEFAULT '',
    days_raw   INT  NOT NULL DEFAULT 90,     -- full-resolution firehose window
    days_1h    INT  NOT NULL DEFAULT 365,    -- hourly rollup (long-range charts)
    days_1d    INT  NOT NULL DEFAULT 730,    -- daily rollup (year-over-year)
    sort_order INT  NOT NULL DEFAULT 100,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- capability → class. An unmapped capability falls back to the 'default' class.
CREATE TABLE IF NOT EXISTS retention_capability (
    capability TEXT PRIMARY KEY,
    class_name TEXT NOT NULL
);

-- Optional per-(entity, capability) override, highest priority (e.g. the solar
-- inverter kept finer than the rest of its class).
CREATE TABLE IF NOT EXISTS retention_override (
    entity_id  TEXT NOT NULL,
    capability TEXT NOT NULL,
    class_name TEXT NOT NULL,
    PRIMARY KEY (entity_id, capability)
);

-- Seed classes (idempotent — only fills missing names; user edits are preserved).
INSERT INTO retention_class (name, label, days_raw, days_1h, days_1d, sort_order) VALUES
    ('default',  'Ostalo',                  90,  365,  365, 100),
    ('solar',    'Solar / proizvodnja',     90, 3650, 3650,  10),
    ('utility',  'Utility / kumulativ',     90,  730, 3650,  20),
    ('climate',  'Klima / okoliš',          90, 1095, 1825,  30),
    ('presence', 'Prisutnost / sigurnost',  90,    0,  365,  40),
    ('discrete', 'Aktuator / diskretno',    90,    0,   90,  50)
ON CONFLICT (name) DO NOTHING;

-- Seed capability → class. Extra/unknown capability names are harmless (never
-- matched); a capability we don't list here just uses 'default'.
INSERT INTO retention_capability (capability, class_name) VALUES
    ('production','solar'), ('energy','solar'),
    ('water','utility'), ('gas','utility'), ('rain','utility'), ('rainfall','utility'),
    ('temperature','climate'), ('humidity','climate'), ('pressure','climate'),
    ('illuminance','climate'), ('wind','climate'), ('wind_speed','climate'),
    ('co2','climate'), ('pm25','climate'), ('battery','climate'),
    ('motion','presence'), ('contact','presence'), ('occupancy','presence'), ('presence','presence'),
    ('on_off','discrete'), ('brightness','discrete')
ON CONFLICT (capability) DO NOTHING;

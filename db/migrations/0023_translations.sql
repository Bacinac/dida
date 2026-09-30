-- Translations for DYNAMIC, adapter-generated display descriptors (entity facet
-- names + device-card headers). Adapters emit a stable ENGLISH descriptor as the
-- name; the UI localises it through this table for the selected language, falling
-- back to the English key when no translation exists. This keeps per-adapter
-- descriptors out of the frontend i18n catalogs (which stay for the fixed UI
-- chrome), so a new adapter never needs a frontend change — its English
-- descriptors just appear in Settings → Prijevodi to be translated.
--
-- The key is the English descriptor itself (so shared descriptors like "Grid
-- voltage" translate ONCE for every adapter that emits them). A user's per-entity
-- rename (entities.label) still overrides everything and is never translated.
CREATE TABLE IF NOT EXISTS translations (
    key        text NOT NULL,   -- the English descriptor (as emitted by the adapter)
    lang       text NOT NULL,   -- 'hr', … ('en' is implicit — the key IS the English)
    value      text NOT NULL,   -- the localised string
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (key, lang)
);

-- Seed the Croatian for the descriptors the solar / iammeter / astro adapters emit,
-- so switching those adapters to an English base is transparent for hr users
-- (EN sees the English key, HR keeps the familiar Croatian). Idempotent.
INSERT INTO translations (key, lang, value) VALUES
    -- shared grid descriptors (solar + iammeter)
    ('Grid voltage',          'hr', 'Napon mreže'),
    ('Grid current',          'hr', 'Struja mreže'),
    ('Grid frequency',        'hr', 'Frekvencija mreže'),
    ('Power factor',          'hr', 'Faktor snage'),
    -- iammeter grid meter
    ('Grid meter',            'hr', 'Mrežno brojilo'),
    ('Grid power (− export)', 'hr', 'Snaga mreže (− izvoz)'),
    ('Imported energy',       'hr', 'Uvezena energija'),
    ('Exported energy',       'hr', 'Izvezena energija'),
    -- solar inverter
    ('Solar inverter',        'hr', 'Solarni pretvarač'),
    ('Current power',         'hr', 'Trenutna snaga'),
    ('Energy today',          'hr', 'Proizvodnja danas'),
    ('Total energy',          'hr', 'Ukupna proizvodnja'),
    ('String 1 voltage (PV1)','hr', 'Napon niza 1 (PV1)'),
    ('String 1 current (PV1)','hr', 'Struja niza 1 (PV1)'),
    ('String 2 voltage (PV2)','hr', 'Napon niza 2 (PV2)'),
    ('String 2 current (PV2)','hr', 'Struja niza 2 (PV2)'),
    ('Inverter temperature',  'hr', 'Temperatura pretvarača'),
    ('Operating hours',       'hr', 'Sati rada'),
    ('Status',                'hr', 'Status'),
    ('Apparent power',        'hr', 'Prividna snaga'),
    ('Reactive power',        'hr', 'Jalova snaga'),
    -- astro
    ('Sun',                   'hr', 'Sunce'),
    ('Clock',                 'hr', 'Sat')
ON CONFLICT (key, lang) DO NOTHING;

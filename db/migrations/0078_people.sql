-- 0078_people.sql
-- The household's address book — the people, not the logins.
--
-- `users` are accounts that sign in and `zones` are places; neither says who the
-- family IS or when they were born. That fact lives in a contacts book, and DIDA
-- is the house's one reader of it: one consent screen, one token to expire, one
-- schedule. OPUS Library asks over HTTP rather than keeping a second copy.
--
-- A row is a person with a birth date that carries a YEAR. A birthday without one
-- is reported by the sync and never stored: the year is the whole reason this is
-- kept, and inventing it would produce a date nobody has ever held.
CREATE TABLE IF NOT EXISTS people (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    -- Accent-, case- and order-free form of `name` (dida_core.people.plain), stored
    -- because a lookup by name is the only handle a caller holding a photo has:
    -- "Bošković" and "Boskovic" must find the same row.
    plain_name  TEXT NOT NULL,
    born_on     DATE NOT NULL,
    -- Google's resourceName. Matching on it first is what makes a rename in the
    -- book a rename here, instead of a stranger plus an orphan.
    contact_id  TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'google',
    -- Whose birthday the house says out loud. The book holds everyone; the kitchen
    -- speaker is for the household, so this is opted into per person and never
    -- inferred from having a birth date.
    announce    BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS people_contact_id_key
    ON people (contact_id) WHERE contact_id <> '';
CREATE INDEX IF NOT EXISTS people_plain_name_idx ON people (plain_name);

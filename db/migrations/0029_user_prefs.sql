-- Per-user UI preferences: preferred theme + language. Persisting them on the
-- user row (not just localStorage, which is per-device) lets a choice made on
-- one device follow the user to the next. NULL = no explicit preference set →
-- the client falls back to its device/localStorage default.
ALTER TABLE users ADD COLUMN IF NOT EXISTS theme  TEXT;  -- 'light' | 'dark' | 'system'
ALTER TABLE users ADD COLUMN IF NOT EXISTS locale TEXT;  -- 'hr' | 'en'

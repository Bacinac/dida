-- Per-user phone-setup tokens: OwnTracks location auth + QR auto-login.
--
-- owntracks_token — the OwnTracks HTTP receiver (dida_api/owntracks.py)
-- authenticates a phone by HTTP Basic `username : owntracks_token`. An
-- endpoint-scoped bearer credential, NOT the login password, so a leaked phone
-- config or setup QR never exposes the account.
--
-- login_token — the phone-setup QR encodes
-- `{public}/api/auth/link?k=<login_token>&next=/onboard`. Scanning it
-- (GET /auth/link) exchanges the token for a normal HttpOnly session cookie and
-- lands the family member on the onboarding page, already signed in. A bearer
-- credential like the wall-panel `panel_token`, so it is rotatable + revocable
-- per user in Settings → Users, and independent of the login password (a
-- password reset does NOT invalidate it).
--
-- Both NULL = not provisioned; both admin-managed in Settings → Users. Unique
-- guards keep two users from ever colliding on a generated value.
ALTER TABLE users ADD COLUMN IF NOT EXISTS owntracks_token TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS login_token TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS users_owntracks_token_key
    ON users (owntracks_token) WHERE owntracks_token IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_login_token_key
    ON users (login_token) WHERE login_token IS NOT NULL;

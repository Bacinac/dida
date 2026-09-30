-- Signing out revokes the one token it signed out with. Bumping the user's
-- token_version would also end their phone app and the car's pairing, which
-- cannot sign in again from the car screen.
CREATE TABLE IF NOT EXISTS revoked_sessions (
    token_hash BYTEA PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS revoked_sessions_expires_at ON revoked_sessions (expires_at);

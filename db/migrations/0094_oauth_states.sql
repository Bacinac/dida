CREATE TABLE IF NOT EXISTS oauth_states (
    nonce_hash    BYTEA PRIMARY KEY,
    integration   TEXT NOT NULL CHECK (integration IN ('contacts', 'smartthings')),
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_version INTEGER NOT NULL,
    token_hash    BYTEA NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_states_expires_at ON oauth_states (expires_at);

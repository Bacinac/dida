-- FCM (Firebase Cloud Messaging) registration tokens — native push for the
-- Android companion app, where Web Push does not work (the WebView has no Push
-- API). One row per app install, tied to the DIDA user signed in on it. The API
-- manages rows (register at the authenticated boundary); the notify adapter
-- reads them to deliver `notify` commands as FCM messages alongside ntfy and Web
-- Push — one fan-out, three transports — and prunes any token FCM reports gone
-- (UNREGISTERED / 404). Sending uses a Google service-account key (FCM HTTP v1),
-- stored like the other signing material, never in the DB.
CREATE TABLE IF NOT EXISTS fcm_tokens (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT NOT NULL UNIQUE,
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ok_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_fcm_tokens_user ON fcm_tokens (user_id);

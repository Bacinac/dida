-- 0083_alert_delivery.sql
-- Who a critical alert reaches, and how to stop one repeating.
--
-- Critical alerts went to every user with the admin role. On the house that is one
-- account, used from a desk, with no phone behind it: "homekit down" fired within a
-- minute of the living-room sensor dropping off and was dropped by the notify
-- adapter with a log line, for 27 hours. Recipients are now chosen notify targets,
-- seeded with what the admin role reached before so nothing changes until someone
-- picks. A recipient list that reaches no device is its own alert (alert_unrouted).
INSERT INTO app_settings (key, value)
SELECT 'alert_recipients',
       COALESCE(jsonb_agg('notify:' || trim(BOTH '_' FROM regexp_replace(lower(trim(username)), '[^a-z0-9_-]+', '_', 'g'))
                          ORDER BY username), '[]'::jsonb)::text
FROM users WHERE role = 'admin'
ON CONFLICT (key) DO NOTHING;

INSERT INTO alert_rules (key, severity, threshold, hold_s, enabled)
VALUES ('alert_unrouted', 'critical', NULL, 300, true)
ON CONFLICT (key) DO NOTHING;

-- A silence stops the push for one alert (rule + scope), not the alert: it still
-- shows, still lands in history. `until` NULL = until that alert resolves.
CREATE TABLE IF NOT EXISTS alert_silences (
    key        TEXT NOT NULL REFERENCES alert_rules (key) ON DELETE CASCADE,
    scope      TEXT NOT NULL DEFAULT '',
    until      TIMESTAMPTZ,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key, scope)
);

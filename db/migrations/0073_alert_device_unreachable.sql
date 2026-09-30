-- Alert when a device's adapter reports it unreachable — the gap the Bara incident
-- exposed: the five existing rules all watch DIDA's own plumbing (adapters, queues,
-- disk), so a healthy adapter with every device behind it dead fired nothing. Held
-- 300s so a node's reconnect blip does not page anyone; warning, not critical, so it
-- shows on the badge without a push for one dropped bulb.
INSERT INTO alert_rules (key, severity, threshold, hold_s) VALUES
    ('device_unreachable', 'warning', NULL, 300)
ON CONFLICT (key) DO NOTHING;

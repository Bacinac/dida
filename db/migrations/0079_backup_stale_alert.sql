-- 0079_backup_stale_alert.sql
-- The failure that says nothing until the day it is needed.
--
-- A reboot raced the NAS coming up, the NFS mount timed out, `nofail` let the
-- machine carry on, and for 23 hours no backup left the box — announced only by a
-- log line every ten minutes that nobody reads. Every other way this breaks (a
-- permission, a broken pg_dump, a scheduler that stopped) looks exactly the same
-- from outside: nothing happens, quietly.
--
-- The measure is the age of the newest backup FILE, because a job that believes
-- it ran and a file that exists are different claims. 36 hours: past one missed
-- daily run, so a single hiccup does not cry wolf, and well short of a second.
INSERT INTO alert_rules (key, severity, threshold, hold_s, enabled)
VALUES ('backup_stale', 'warning', 36, 0, true)
ON CONFLICT (key) DO NOTHING;

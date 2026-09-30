-- 0008 — the adapter runner journalled a failed command as `command_fail` while
-- the device timeline had words only for `command_failed`, so those rows read as
-- a raw key. The kind is `command_failed` everywhere now (JournalKind); the rows
-- written under the old spelling follow it. Rerunning changes nothing.
ALTER TABLE device_events UPDATE kind = 'command_failed' WHERE kind = 'command_fail';

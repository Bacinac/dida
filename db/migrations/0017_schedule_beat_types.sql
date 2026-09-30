-- Schedules v2 — "Calendar Beat" trigger types (mirrors TaskStreamer beats,
-- light). `kind` widens from (season, cron) to:
--   calendar → fire a momentary `button` tick on a recurrence pattern
--     (once/daily/weekly/monthly/yearly + interval + weekdays + monthly pattern);
--     params = a dateutil-rrule-shaped recurrence_config.
--   solar    → fire at a sun event (dawn/sunrise/sunset/dusk ± offset).
--   lunar    → fire on a moon phase (new/first_quarter/full/last_quarter).
--   season   → a `schedule_active` boolean, true across an MM-DD..MM-DD window.
-- The old free-form `cron` kind is dropped (its friendly successor is calendar).
ALTER TABLE schedules DROP CONSTRAINT IF EXISTS schedules_kind_check;
ALTER TABLE schedules
    ADD CONSTRAINT schedules_kind_check
    CHECK (kind IN ('calendar', 'solar', 'lunar', 'season'));

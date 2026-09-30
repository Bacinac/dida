-- Calendar/schedule definitions — DIDA's light "beat" scheduler (the native
-- take on HA local_calendar + a periodic-task creator). Each enabled row is
-- turned by the `calendar` adapter into one entity `calendar:<slug(name)>`:
--
--   kind='season' → a `schedule_active` boolean, true while today is inside a
--     (yearly-recurring) MM-DD..MM-DD window. Wrap across New Year is allowed
--     (end < start). params: {"start":"05-01","end":"09-05"}.
--   kind='cron'   → a momentary `button` "tick" fired when a 5-field crontab
--     expression matches the current minute — a periodic automation trigger.
--     params: {"expr":"30 6 * * 1,3,5"}.
--
-- Definitions live here; the live value lives in current_state (single source
-- of truth), exactly like virtual_entities.
CREATE TABLE IF NOT EXISTS schedules (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text        NOT NULL,
    kind        text        NOT NULL CHECK (kind IN ('season', 'cron')),
    params      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    enabled     boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Schedules v4 — beats are day-level `schedule_active` booleans, no time-of-day
-- (automations add the time). The from–to is a yearly-repeating window, so the
-- separate 'window' recurrence type is gone: fold it into a `daily` beat whose
-- start_date/end_date carry the window (their month-day recurs every year).
--   window {start:'MM-DD', end:'MM-DD'} → daily {start_date:'YYYY-MM-DD', end_date:'YYYY-MM-DD'}
UPDATE schedules
   SET params = jsonb_build_object(
         'recurrence_type', 'daily',
         'start_date', extract(year FROM now())::int || '-' || (params->>'start'),
         'end_date',   extract(year FROM now())::int || '-' || (params->>'end')
       )
 WHERE params->>'recurrence_type' = 'window';

-- `time` is no longer part of a schedule (automations own the clock) — drop it.
UPDATE schedules SET params = params - 'time' WHERE params ? 'time';

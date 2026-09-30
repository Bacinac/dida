-- Schedules v3 — collapse to a single `calendar` kind. Sun/moon timing is now
-- handled by automations on the astro entities (sun_elevation), not beat types;
-- the season "window" folds into calendar as recurrence_type='window'.
--   * season rows → calendar with recurrence_type='window'
--   * solar / lunar rows → dropped (superseded by automations)
UPDATE schedules
   SET kind = 'calendar',
       params = jsonb_set(coalesce(params, '{}'::jsonb), '{recurrence_type}', '"window"')
 WHERE kind = 'season';

DELETE FROM schedules WHERE kind IN ('solar', 'lunar');

ALTER TABLE schedules DROP CONSTRAINT IF EXISTS schedules_kind_check;
ALTER TABLE schedules ADD CONSTRAINT schedules_kind_check CHECK (kind = 'calendar');

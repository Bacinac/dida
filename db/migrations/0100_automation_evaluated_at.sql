-- When a fire last reached the rule, acted on or not. last_triggered_at moves only
-- when actions ran, so a boot could not tell a hold that expired into a blocked
-- condition (or a script that chose to do nothing) from one that expired while the
-- service was down, and re-fired every such hold on each deploy. Boot reconstruction
-- compares this against the trigger's moment instead.
--
-- Seeds from last_triggered_at, the evidence the old check used: for one boot it
-- behaves as before, then it is exact. Idempotent.
ALTER TABLE automations ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ;
UPDATE automations SET last_evaluated_at = last_triggered_at WHERE last_evaluated_at IS NULL;

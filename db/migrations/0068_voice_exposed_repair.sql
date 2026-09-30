-- Repair rows the engine stamped with a concrete `voice_exposed` on announce.
--
-- 0062 made the column tri-state (NULL follows the rule) and collapsed the old
-- curation, but the engine's EntityInfo upsert kept writing a concrete value on
-- INSERT (`voice_exposed = device_type='light' AND NOT diagnostic`). So every
-- entity first announced AFTER 0062 landed with an explicit override: a new
-- controllable switch or cover as `false` = "keep out" and never reached the
-- assistant, and a light frozen `true` even if it later stopped being one. The
-- insert has since been fixed to leave the column NULL; this collapses the values
-- it wrote in the meantime back onto the rule.
--
-- Same predicate as 0062's collapse: discard every `false` (it only ever meant
-- "untouched"), and keep a `true` only when it is both meaningful (carries on_off
-- or hvac_mode) AND a genuine deviation the rule would not already cover.
-- Idempotent — re-running only ever touches rows that still carry a stale value.
UPDATE entities SET voice_exposed = NULL
WHERE voice_exposed IS NOT NULL
  AND NOT (
        voice_exposed IS true
    AND (capabilities ? 'on_off' OR capabilities ? 'hvac_mode')
    AND NOT (category = 'control'
             AND NOT diagnostic
             AND device_type IN ('light', 'switch', 'cover'))
  );

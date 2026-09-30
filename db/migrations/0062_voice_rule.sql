-- Voice exposure becomes a RULE with per-entity overrides, instead of a checklist.
--
-- 0058 introduced `voice_exposed` as a hand-curated keep-set: every entity carried an
-- explicit true/false and a new device arrived silently switched off, so getting it
-- into Google Home meant finding it in Settings → Adapters and clicking a 🎙. With 59
-- eligible entities that is a chore that exists only because nobody wrote the rule
-- down. The rule was always the same sentence: a controllable light, switch or cover
-- that the house actually operates.
--
-- So: `voice_exposed` becomes TRI-STATE — NULL follows the rule, true/false are
-- deliberate overrides — and `voice_effective` computes the answer once, in the
-- database. The Matter bridge (TypeScript) and the UI (Svelte) both read that column
-- rather than each restating the rule in their own language, which is the drift this
-- is meant to prevent.
--
-- Locks are deliberately outside the rule: a spoken command carries no
-- authentication, and a lock, unlike a gate, has no self-closing behaviour to fall
-- back on. Someone who wants one voiced can still force it in per entity.
ALTER TABLE entities ALTER COLUMN voice_exposed DROP DEFAULT;
ALTER TABLE entities ALTER COLUMN voice_exposed DROP NOT NULL;

ALTER TABLE entities ADD COLUMN IF NOT EXISTS voice_effective BOOLEAN
    GENERATED ALWAYS AS (
        COALESCE(
            voice_exposed,
            category = 'control'
                AND NOT diagnostic
                AND device_type IN ('light', 'switch', 'cover')
                AND (capabilities ? 'on_off' OR capabilities ? 'hvac_mode')
        )
    ) STORED;

-- Collapse the old curation onto the rule, keeping only genuine deviations.
--
-- `false` is discarded wholesale: 0058 defaulted every row to false, so a false says
-- "nobody ever touched this", not "keep this out". Reading it as a deliberate
-- exclusion would freeze the old keep-set in place and make the rule inert — the
-- exact outcome this migration exists to undo.
--
-- A `true` survives only when it is BOTH still meaningful and a real decision:
--   * meaningful = the entity carries on_off or hvac_mode. The bridge has always
--     required that, so a `true` on anything else was never exposing anything. This
--     house has 19 of them: 0058 seeded every `device_type='light'` row, and at the
--     time nineteen ESPHome Reset/Restart buttons were mis-typed as lights. The type
--     was corrected later; the flag was left behind. It never reached Google Home.
--   * a real decision = the rule would NOT have covered it anyway. The air
--     conditioner is typed `sensor` and voiced on purpose; that one stays.
UPDATE entities SET voice_exposed = NULL
WHERE voice_exposed IS NOT NULL
  AND NOT (
        voice_exposed IS true
    AND (capabilities ? 'on_off' OR capabilities ? 'hvac_mode')
    AND NOT (category = 'control'
             AND NOT diagnostic
             AND device_type IN ('light', 'switch', 'cover'))
  );

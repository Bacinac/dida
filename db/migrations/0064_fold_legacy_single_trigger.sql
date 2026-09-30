-- Fold the pre-multi-trigger `definition.trigger` into `definition.triggers`.
--
-- The model carried both shapes and read the singular only when the plural was
-- absent or empty. Dropping that fallback would silently disarm every rule still
-- written the old way (msgspec ignores an unknown key, so the rule would decode
-- with no trigger at all), so the rows move first and the code follows.
--
-- Idempotent: after the fold no row carries `trigger`, and re-running matches
-- nothing. A row holding BOTH keeps its plural list — that is the precedence the
-- old accessor had — and just loses the dead singular.

UPDATE automations
SET definition = jsonb_set(definition - 'trigger', '{triggers}',
                           jsonb_build_array(definition -> 'trigger'))
WHERE definition ? 'trigger'
  AND definition -> 'trigger' <> 'null'::jsonb
  AND coalesce(CASE WHEN jsonb_typeof(definition -> 'triggers') = 'array'
                    THEN jsonb_array_length(definition -> 'triggers') END, 0) = 0;

UPDATE automations
SET definition = definition - 'trigger'
WHERE definition ? 'trigger';

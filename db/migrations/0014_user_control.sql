-- Per-user access rules — Phase 2 of the per-user access policy.
--
-- Two real boundaries, enforced server-side, sharing one rules table keyed by
-- `kind`:
--   kind='control' — device operation. Baseline `users.can_control` + flip rules.
--                    Enforced at /command (dida_api/permissions.py): the command
--                    is rejected before it reaches the bus.
--   kind='view'    — visibility. Hide rules only (no baseline; default = see all).
--                    Enforced at every read path (dida_api/visibility.py): /state,
--                    /entities, /history and the WS firehose filter hidden entities
--                    out, so the client never receives them — the whole UI inherits
--                    the hiding for free. A hidden entity is also uncontrollable.
--
-- control resolution for a command on entity E, capability C:
--   matched = rule(entity=E) OR rule(area=E.area_id) OR rule(capability=C)
--   allowed = (can_control != matched)          -- flip: rule denies (baseline true)
--                                                   or grants (baseline false)
-- view resolution: entity hidden iff any view rule matches it (entity/area/cap).
-- Admins bypass both entirely.
ALTER TABLE users ADD COLUMN IF NOT EXISTS can_control BOOLEAN NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS user_access_rules (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind    TEXT   NOT NULL CHECK (kind IN ('control', 'view')),
    scope   TEXT   NOT NULL CHECK (scope IN ('entity', 'area', 'capability')),
    -- entity_id (text), area id (as text), or capability name — matched by string.
    ref     TEXT   NOT NULL,
    PRIMARY KEY (user_id, kind, scope, ref)
);

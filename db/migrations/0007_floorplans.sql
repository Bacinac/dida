-- 0004 — user-managed floor plans.
--
-- A floor is a named level of the home. Its raster image is an uploaded
-- SETUP-TIME SCAFFOLD (stored under /state/floorplan): the vision service turns
-- it into room polygons, after which it is only ever a dim reference while
-- editing. The CONSUMER floorplan renders pure vector (areas.fp_poly), never the
-- bitmap — so a plan carries no furniture/text/dimension clutter.
--
-- areas.fp_floor and entities.fp_floor already store a floor KEY; this table
-- makes that key first-class (name, order, scaffold image) instead of hardcoding
-- the floor list in the UI.
CREATE TABLE IF NOT EXISTS floors (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key        TEXT NOT NULL UNIQUE,       -- stable slug referenced by areas/entities.fp_floor
    name       TEXT NOT NULL,              -- user-facing label (renameable; not translated)
    sort_order INT  NOT NULL DEFAULT 0,    -- tab order
    img_path   TEXT,                       -- scaffold raster filename under /state/floorplan (NULL until uploaded)
    img_w      INT,                        -- native px width  (aspect ratio + %↔px), NULL until known
    img_h      INT,                        -- native px height
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Floors are the installation's own: created in Settings by uploading a plan,
-- which promotes the floor to vectorization. img_path NULL renders a floor from
-- its traced polygons alone.

-- Route/page keys are ALWAYS English, whatever the UI language: /ulaz and
-- /kamere become /entry and /cameras, and the per-user page grants stored under
-- the old keys move with them — a scoped login must keep exactly the access it
-- had, under the new names.
UPDATE users
   SET allowed_pages = array_replace(array_replace(allowed_pages, 'ulaz', 'entry'), 'kamere', 'cameras')
 WHERE allowed_pages IS NOT NULL
   AND ('ulaz' = ANY(allowed_pages) OR 'kamere' = ANY(allowed_pages));

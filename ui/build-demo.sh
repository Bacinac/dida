#!/usr/bin/env bash
# Build the public, backend-less DIDA demo.
#
# Same frontend as production; only its network layer is swapped (static/demo-net.js
# patches fetch + WebSocket) and it is emitted as a static SPA. Visitors can browse,
# edit, drag and toggle — every write lands in an in-memory copy of a recorded,
# anonymised house and disappears on reload.
#
#   ui/build-demo.sh <fixtures.json> [out-dir] [floorplan-dir] [snapshot-dir]
#
# Fixtures come from scripts/record-demo-api.py (drives the real app against a
# running instance and captures every /api GET). floorplan-dir holds the plan
# images (<floor-key>.png) the fixtures reference.
set -euo pipefail

FIXTURES="${1:?usage: build-demo.sh <fixtures.json> [out-dir] [floorplan-dir]}"
OUT="${2:-$(cd "$(dirname "$0")" && pwd)/demo-dist}"
PLANS="${3:-}"
SNAPS="${4:-}"
UI="$(cd "$(dirname "$0")" && pwd)"

[[ -f "$FIXTURES" ]] || { echo "no fixtures at $FIXTURES" >&2; exit 1; }

cd "$UI"
cp "$FIXTURES" static/demo-fixtures.json

# Inject the shim into the placeholder app.html carries for exactly this purpose,
# writing a SEPARATE template (svelte.config.js points the demo build at it) —
# app.html itself is tracked, and a run that dies between edit and restore left
# the shim in the production source.
trap 'rm -f src/app.demo.html static/demo-fixtures.json' EXIT
sed 's|<!--DEMO_NET-->|<script src="/demo-net.js"></script>|' src/app.html > src/app.demo.html
grep -q 'demo-net.js' src/app.demo.html || { echo "app.html has no <!--DEMO_NET--> placeholder" >&2; exit 1; }

DIDA_DEMO=1 npx vite build

rm -rf "$OUT"
mv build "$OUT"

# Images the app loads with <img src>, which never reaches the fetch shim.
# Floor plans ship as real files; camera snapshots deliberately DO NOT — a
# public demo has no business serving frames from someone's cameras — so every
# /api/camera/* path resolves to a placeholder instead.
if [[ -n "$PLANS" && -d "$PLANS" ]]; then
    mkdir -p "$OUT/api/floorplan"
    cp "$PLANS"/*.png "$OUT/api/floorplan/" 2>/dev/null || true
fi
cp "$UI/static/demo-no-camera.png" "$OUT/demo-no-camera.png" 2>/dev/null || true

# Curated stills, one per camera, named after the camera's FULL entity id
# (baba:<uuid>.jpg, frigate:<slug>.jpg): hand-picked frames of the owner's own
# property, no people, no readable plates — never a live feed. The name carries
# the adapter prefix rather than assuming one, because the wall mixes sources:
# a hardcoded "baba:" left every Frigate camera showing the no-feed card.
#
# They are written as REAL FILES at the exact path the app requests
# (/api/camera/<entity-id>/snapshot) rather than as _redirects rules: the edge
# matcher treats the percent-encoded colon inconsistently, and a rule with two
# splats is rejected outright, so redirects silently fell through to the
# placeholder. A file always wins. `_headers` supplies the content type, since
# the path carries no extension.
if [[ -n "$SNAPS" && -d "$SNAPS" ]]; then
    for f in "$SNAPS"/*.jpg; do
        [[ -e "$f" ]] || continue
        eid="$(basename "$f" .jpg)"
        mkdir -p "$OUT/api/camera/$eid"
        cp "$f" "$OUT/api/camera/$eid/snapshot"
    done
fi

# Cameras without a curated still get the deliberate "no feed" card — again as a
# real file, not a redirect: Pages evaluates _redirects BEFORE static assets, so
# a catch-all /api/camera/* rule swallowed the stills we had just written.
cp "$UI/static/demo-no-camera.png" "$OUT/demo-no-camera.png" 2>/dev/null || true
for cam in $(python3 -c "
import json
d = json.load(open('$FIXTURES'))
print(' '.join(sorted({r['entity_id'] for r in d.get('/state', [])
                       if r.get('capability') == 'camera'})))"); do
    dir="$OUT/api/camera/$cam"
    [[ -f "$dir/snapshot" ]] && continue
    mkdir -p "$dir"
    cp "$UI/static/demo-no-camera.png" "$dir/snapshot"
done

{
    for f in "$OUT/api/floorplan"/*.png; do
        [[ -e "$f" ]] || continue
        b="$(basename "$f" .png)"
        echo "/api/floorplan/$b  /api/floorplan/$b.png  200"
    done
} > "$OUT/_redirects"

printf '/api/camera/*\n  Content-Type: image/jpeg\n' > "$OUT/_headers"

echo "demo built → $OUT"

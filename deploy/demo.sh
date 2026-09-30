#!/usr/bin/env bash
# Refresh the public, backend-less demo at https://demo-dida.boskovic.biz.
#
# It is the REAL frontend: the same SvelteKit app, built as a static SPA with
# its network layer swapped (ui/static/demo-net.js). Visitors browse, edit,
# drag and toggle; every write lands in an in-memory copy of a recorded house
# and is gone on reload. Nothing here can reach a running DIDA.
#
# Run after a deploy so the demo always shows the frontend that just shipped:
#   ./deploy/demo.sh
#
# Data comes from the DEV instance (which carries an anonymised snapshot of the
# real house), NOT from production. Re-recording on every run means a UI change
# that needs a new endpoint can't silently ship a half-empty demo.
#
# Needs: a running dev stack, ./.cf-token (Cloudflare Pages:Edit), docker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

UI_URL="${DIDA_DEMO_UI:-http://localhost:5273}"
# The recorder signs in as the dev admin. Keep that password in a 0600 file
# rather than in a crontab line, which is world-readable on most systems and
# ends up in shell history and process listings.
PASS_FILE="${DIDA_DEMO_PASS_FILE:-$ROOT/.demo-pass}"
DEMO_PASS="${DIDA_DEMO_PASS:-}"
if [[ -z "$DEMO_PASS" && -s "$PASS_FILE" ]]; then
    DEMO_PASS="$(tr -d ' \t\n\r' < "$PASS_FILE")"
fi
[[ -n "$DEMO_PASS" ]] || { echo "no dev admin password: set DIDA_DEMO_PASS or write $PASS_FILE" >&2; exit 1; }
CF_TOKEN_FILE="${DIDA_CF_TOKEN:-$ROOT/.cf-token}"
CF_ACCOUNT="${CLOUDFLARE_ACCOUNT_ID:-3e7a41dadfd065fac67c6922c3f3b43e}"
PROJECT="${DIDA_DEMO_PROJECT:-dida-demo}"
WORK="$(mktemp -d)"
# The build/deploy containers write as root, so hand the cleanup to a container
# too — a plain rm -rf here fails on their files and leaves the tree behind.
ENGINE_HELD=0
cleanup() {
    # The engine comes back whatever happened above — a failed gate must not leave
    # the dev instance with nothing writing state.
    if [[ "$ENGINE_HELD" == 1 ]]; then
        echo "== releasing the engine =="
        (cd "$ROOT" && docker compose start engine >/dev/null) || true
    fi
    docker run --rm -v "$WORK":/w alpine:3 sh -c 'rm -rf /w/* /w/.[!.]* 2>/dev/null' || true
    rmdir "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

[[ -s "$CF_TOKEN_FILE" ]] || { echo "no Cloudflare token at $CF_TOKEN_FILE" >&2; exit 1; }

# Dev runs the real adapters, so the anonymised snapshot decays: every live report
# writes today's truth back over yesterday's rewrite, and a camera that announced
# itself after the last refresh carried its real host straight into the fixtures.
# Re-running the passes costs seconds and is idempotent.
#
# But re-running them against a database still being written to is a race, and it
# lost: the cast adapter re-announced a child's bedroom TV between the rewrite and
# the gate, and the deploy ended on `LEAK: entities.name=1`. Each such round also
# left a numbered orphan behind (tv_child_room2, tv_child_room3 …) as the rename
# collided with the previous run's result.
#
# So the writer is held still for the window. The ENGINE is the only thing that
# turns bus traffic into `entities`/`current_state` rows (the two API paths that
# touch them are a human renaming or removing a device, which does not happen
# mid-recording), so stopping that one container is enough — the adapters keep
# talking to their devices, and `dida.state.>` is a durable JetStream consumer, so
# their reports queue and drain when it returns. Nothing is lost; the database
# simply stops moving while it is being read.
echo "== holding the engine still (the only writer of entities/current_state) =="
(cd "$ROOT" && docker compose stop engine >/dev/null)
ENGINE_HELD=1

echo "== re-anonymising dev data =="
python3 "$ROOT/scripts/refresh-demo-data.py" --anonymise-only

# The dev vite server can sit wedged behind a stale error overlay (fetchModule
# timeouts when builds ran alongside it) — the overlay eats the recorder's login
# click and the refresh dies on a timeout. A restart is ~700 ms and guarantees a
# clean bundle before the browser ever opens.
echo "== restarting dev ui for a clean recorder run =="
(cd "$ROOT" && docker compose restart ui >/dev/null)
for _ in $(seq 1 60); do
    curl -fsS -o /dev/null "$UI_URL" 2>/dev/null && break
    sleep 1
done

echo "== recording API fixtures from $UI_URL =="
docker run --rm --network host \
    -v "$ROOT/scripts":/s:ro -v "$WORK":/out -w /s \
    -e DIDA_DEMO_UI="$UI_URL" -e DIDA_DEMO_PASS="$DEMO_PASS" -e DIDA_DEMO_OUT=/out \
    mcr.microsoft.com/playwright/python:v1.49.1-noble \
    sh -lc "pip -q install 'playwright==1.49.1' >/dev/null 2>&1; python record-demo-api.py"

# Live adapter status echoes hosts the DB never held (an adapter names the AVR
# it is connected to), so those identifiers can only be caught here, after the
# recording. The container wrote the file as root — hand it back first.
echo "== scrubbing fixture identifiers =="
docker run --rm -v "$WORK":/w alpine:3 chown "$(id -u):$(id -g)" /w/api-fixtures.json
python3 "$ROOT/scripts/refresh-demo-data.py" --scrub-fixtures "$WORK/api-fixtures.json"

# A demo that leaked a household name would be unfixable once cached publicly.
echo "== privacy gate =="
# Diacritics and double-encoded descriptors both hid a place name from the
# old inline check; the gate now lives in one place and decodes both.
python3 "$ROOT/scripts/demo-privacy-gate.py" "$WORK/api-fixtures.json"
# The same deny list every public push answers to (the live household, plates,
# MACs, secrets), found through the clone's own config: required, not optional.
GATE=$(git -C "$ROOT" config --get publicgate.command) \
    || { echo "no publicgate.command in this clone — the demo is not published unchecked" >&2; exit 1; }
"$GATE" scan --files "$WORK/api-fixtures.json"

# Everything below reads the fixtures, not the database — the engine can catch up
# on the backlog while the demo builds.
echo "== releasing the engine =="
(cd "$ROOT" && docker compose start engine >/dev/null)
ENGINE_HELD=0

echo "== plan images =="
mkdir -p "$WORK/plans"
for key in $(python3 -c "
import json;print(' '.join(f['key'] for f in json.load(open('$WORK/api-fixtures.json')).get('/floors', [])))"); do
    docker exec dida-api sh -c "cat /state/floorplan/${key}.png" > "$WORK/plans/${key}.png" 2>/dev/null || true
done

echo "== building demo =="
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$ROOT/ui":/app -v "$WORK":/w -v "$ROOT/demo-snapshots":/snaps:ro -w /app node:24 \
    sh -lc "./build-demo.sh /w/api-fixtures.json /w/demo-dist /w/plans /snaps" | tail -1

echo "== deploying to $PROJECT =="
docker run --rm -v "$WORK/demo-dist":/site -w /site \
    -e CLOUDFLARE_API_TOKEN="$(tr -d ' \n' < "$CF_TOKEN_FILE")" \
    -e CLOUDFLARE_ACCOUNT_ID="$CF_ACCOUNT" node:24 \
    sh -lc "npx -y wrangler@latest pages deploy /site --project-name $PROJECT 2>&1 | grep -E 'Deployment complete|error'"

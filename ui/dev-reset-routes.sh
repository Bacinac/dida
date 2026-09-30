#!/usr/bin/env bash
# Clean-regenerate the SvelteKit route table after ADDING or REMOVING a route
# directory (src/routes/**/+page.svelte). A plain `docker compose restart ui`
# keeps the container-local `.svelte-kit/` cache, so SvelteKit's node
# renumbering leaves the browser mapping the new route number onto a STALE
# cached module — e.g. the Schedules link opening the TTS page. Wiping the cache
# forces fresh module hashes and a clean browser reload on reconnect.
#
# Run from the repo root (or anywhere — it targets the dida-ui container):
#   ./ui/dev-reset-routes.sh
set -euo pipefail

echo "Clearing SvelteKit route cache in dida-ui…"
docker exec dida-ui rm -rf /app/.svelte-kit
echo "Restarting ui (regenerates .svelte-kit fresh)…"
docker compose restart ui
echo "Done. The browser full-reloads on reconnect with a consistent bundle."

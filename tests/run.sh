#!/usr/bin/env sh
# Run the DIDA test suite (pytest) inside the service images.
#
#   sh tests/run.sh
#
# The image supplies the DEPENDENCIES; OUR packages (dida_core, dida_api, …) are
# resolved from the WORKING TREE via PYTHONPATH, ahead of the copies baked into the
# image — so the gate tests the source you're about to push, no rebuild needed.
# pytest + pytest-cov + coverage are installed at TEST TIME (uv, cached), never into
# the production image. Each group writes a coverage data file; they're combined at
# the end and the run FAILS if total line coverage drops below COV_MIN.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Per-run coverage dir (PID-suffixed) so CONCURRENT gate runs — e.g. a parallel
# session's own pre-push hook — don't clobber each other's data files under the
# shared /w mount (a shared .cov gave a partial combine → false low coverage).
COVN=".cov-$$"
COV="$ROOT/$COVN"
# Combined line-coverage floor. RATCHET (raise as coverage grows, never lower to
# hide a regression). RE-BASELINED 35→33 when 6 AV adapters entered scope: their
# whole packages (untested adapter.py included, honestly) grew the denominator
# +2621 stmts, so the % fell 36.4→34.0 even though absolute covered lines ROSE
# (+668). Whole-package --cov is kept deliberately — scoping to mapping.py would
# hide the untested adapter.py lifecycle the number is meant to expose.
# RATCHETED 37→41 after the core-backbone wave (starlark_worker 0→98, bus 33→85,
# discovery/adapter_runner/presence/registry/automations →100, health →89): 38.4→41.5%.
# RATCHETED 41→50 after the route+mapping wave: backup 21→80 (real pg_dump/restore
# round-trip), floors/energy →100, owntracks 57→97, users 55→99, esphome/smartthings/
# mqtt mapping →100. Combined 41.5→50.4% — all in already-in-scope packages.
# RATCHETED 50→51 after the CodeQuality-audit remediation waves (argless-setter guard,
# hold-cancel, runner dispatch, engine metadata/republish, LLM-key encryption, calendar
# recurrence + boundary tests): combined →51.3%.
# RE-BASELINED 51→50 when the baba adapter entered scope (adapter_test baba): its whole
# ~600-LOC package joins the denominator — mostly the NATS connect/reconnect/roster
# lifecycle no unit test drives — so the % fell 51.2→50.6 even though covered lines
# ROSE (the person-mirror mapping is tested). Same deliberate trade as the 35→33
# AV-adapter re-baseline: whole-package --cov keeps the untested lifecycle VISIBLE in
# the number rather than hiding it by scoping to the mapping.
# RATCHETED 50→52 with the assistant suites: the one LLM surface with WRITE access
# (publishes commands, inserts automations) had zero tests, so its tool dispatch,
# per-user boundary and generated prompt vocabulary were entirely ungated. Combined
# 50.6→53.0%.
# RATCHETED 52->53 with the help-corpus suite and the legacy-trigger removal: the
# explain-itself corpus (DIDA's only documentation) went from ungated to covered,
# and dropping the singular-trigger fallback took a branch out of the denominator.
# Combined 53.0->54.0%.
# RE-BASELINED 53->46 (2026-08-07) when the denominator was made HONEST. `--cov=<module>`
# for a package no test imports contributes NOTHING — it only warns — so 2 543 statements
# of shipping lifecycle (frigate 511, homekit 262, broadlink 240, govee 214, notify 185,
# ecowitt 195, announce 166, midea 156, virtual 137) were simply absent from the number
# that claims to measure us, and 7 more packages that DO have tests were absent too.
# Adding them by PATH: denominator 17 827 -> 22 005, and the figure fell 55.2 -> 47.0.
# COVERED LINES ROSE in the same move (9 840 -> 10 339). The percentage dropped because
# the sample stopped being chosen favourably — same deliberate trade as the 35->33 and
# 51->50 re-baselines above. Ratchet from here; never lower it to hide a regression.
#
# 46 -> 50 (measured 51.2) after frigate and govee — the two largest shipping
# adapters with NO test at all — went 0% -> 38% and 0% -> 46%. The ratchet is the
# point: a floor left below what the suite actually achieves lets the next change
# give it back silently.
#
# 50 -> 51 (measured 52.4) after the lifecycle half of dlna, volumio, tuya and
# androidtv. Deliberately one point under: these suites cover DECISIONS and stop at
# the I/O boundary, so the figure will not keep climbing on its own, and a floor
# with no headroom turns an unrelated refactor into a red gate.
#
# 51 -> 52 (measured 53.4) once broadlink and homekit went 0% -> 30%. No adapter is
# at zero any more, which is the milestone worth naming: every shipping adapter now
# has at least its translation layer under test.
COV_MIN=56

# A failed run exits long before the combine; the trap takes its data files with it.
# A run killed outright leaves its directory, reaped here once its PID is gone.
for orphan in "$ROOT"/.cov-*; do
  [ -d "$orphan" ] || continue
  kill -0 "${orphan##*-}" 2>/dev/null || rm -rf "$orphan"
done
rm -rf "$COV"; mkdir -p "$COV"
cleanup() { :; }
trap 'cleanup; rm -rf "$COV"' EXIT
trap 'cleanup; rm -rf "$COV"; exit 143' INT TERM HUP
PYDEPS="pytest pytest-cov pytest-asyncio coverage"
INSTALL="uv pip install --python /opt/venv/bin/python -q $PYDEPS >/dev/null 2>&1"
RUN="docker run --rm -e UV_CACHE_DIR=/uvcache -v dida-uv-cache:/uvcache -v $ROOT:/w -w /w --entrypoint sh"

# Supply chain, before any test: a secret or a known-vulnerable dependency stops the
# push instead of being found after it. Both scan what git would ship (tracked and
# untracked-not-ignored files) — a gitignored per-host .env or build output is this
# box's business, not the push's. osv-scanner reads uv.lock and both npm lockfiles.
echo "== supply chain: gitleaks (secrets) + osv-scanner (dependency CVEs) =="
SHIPPED="git -C $ROOT ls-files -z --cached --others --exclude-standard"
UNPACK='mkdir /tmp/s && tar -x -C /tmp/s'
$SHIPPED | tar -C "$ROOT" --null --ignore-failed-read -T - -cf - 2>/dev/null \
  | docker run --rm -i -v "$ROOT/.gitleaks.toml:/gitleaks.toml:ro" --entrypoint sh ghcr.io/gitleaks/gitleaks:latest \
      -c "$UNPACK && gitleaks dir --no-banner --redact --log-level warn --config /gitleaks.toml /tmp/s" \
  || { echo "  FAILED: gitleaks found a secret in the tree"; exit 1; }
$SHIPPED | tar -C "$ROOT" --null --ignore-failed-read -T - -cf - 2>/dev/null \
  | docker run --rm -i --entrypoint sh ghcr.io/google/osv-scanner:latest \
      -c "$UNPACK && /osv-scanner scan --recursive /tmp/s >/tmp/osv.txt 2>&1 || { grep -v '^Scan\|^Starting\|^End status' /tmp/osv.txt; exit 1; }" \
  || { echo "  FAILED: osv-scanner found a vulnerable dependency"; exit 1; }

# Working-tree source roots (prepended to PYTHONPATH so they shadow the baked pkgs).
CORE="/w/core/src"
PP_API="$CORE:/w/services/api/src"
PP_AUTO="$CORE:/w/services/automation/src"
PP_ENGINE="$CORE:/w/services/engine/src"
PP_JOURNAL="$CORE:/w/services/journal/src"

# Core lives INSIDE dida/base (every service image is FROM it), so a service built
# against an older base runs old core — and this gate can't see it: the suites load
# our packages from the working tree via PYTHONPATH, so they pass while the running
# containers still import yesterday's dida_core (measured: a new core symbol gave
# every service an ImportError at boot, with a green gate behind it). Rebuild the
# base whenever core/ or the migrations moved. It is cached, so the no-op case costs
# about a second.
base_built=$(docker image inspect dida/base:latest --format '{{.Created}}' 2>/dev/null || echo "")
if [ -z "$base_built" ] || [ -n "$(find "$ROOT/core" "$ROOT/db/migrations" "$ROOT/ch/migrations" "$ROOT/docker/base.Dockerfile" -type f -newermt "$base_built" -print -quit 2>/dev/null)" ]; then
  echo "== core changed since dida/base was built — rebuilding it =="
  (cd "$ROOT" && docker compose --profile bases build base) || {
    echo "FAILED: dida/base rebuild failed — services would run stale core" >&2
    exit 1
  }
  echo "== rebuilding the service images on top of it =="
  (cd "$ROOT" && docker compose --profile "*" build) || {
    echo "FAILED: service rebuild on the new base failed" >&2
    exit 1
  }
fi

# Preflight: FAIL LOUD if any image a suite needs is missing — a silently-skipped
# suite lets a green gate hide untested code. (The fresh-clone whole-skip lives in
# scripts/pre-push-tests.sh, which only invokes this once dida/base is built; here
# we REQUIRE every image, so a partial build is a loud failure, never a quiet pass.)
REQUIRED="base api automation engine journal netmgr adapter-notify adapter-frigate adapter-broadlink adapter-homekit adapter-govee adapter-announce adapter-ecowitt adapter-mqtt adapter-shelly adapter-esphome adapter-tuya adapter-smartthings \
  adapter-androidtv adapter-samsungtv adapter-opus adapter-cast adapter-dlna adapter-denon adapter-harmony adapter-heos adapter-volumio \
  adapter-calendar adapter-contacts adapter-baba adapter-roidmi adapter-dreame adapter-panasonic adapter-cloudflare planvision"
missing=""
for name in $REQUIRED; do
  docker image inspect "dida/$name:latest" >/dev/null 2>&1 || missing="$missing dida/$name"
done
if [ -n "$missing" ]; then
  echo "FAILED: test image(s) not built —$missing" >&2
  echo "  build them first (docker compose --profile home --profile presence build) — refusing to skip" >&2
  echo "  suites silently and let a green gate pass over untested code." >&2
  exit 1
fi

# The preflight above guards IMAGES; nothing guarded the SUITE LIST itself, which is
# hand-written below. So a new tests/test_*.py was inert until someone remembered to
# add it — passing locally, invisible to the gate, indistinguishable from covered.
# That is the very failure the image preflight exists to prevent, and it had already
# happened twice (test_calendar.py + test_planvision.py, 240 lines, never run since
# they were written). Cross-check the directory against this file instead of trusting
# memory: an unreferenced suite now fails the gate LOUD.
unlisted=""
for f in "$ROOT"/tests/test_*.py; do
  base=$(basename "$f")
  grep -qF "$base" "$0" && continue
  # …or named indirectly: the adapter_test helper builds `tests/test_<name>_mapping.py`
  # from its FIRST argument, so the literal filename never appears for those suites.
  # Any further arguments ARE literal paths and are caught by the grep above — the
  # anchor here must therefore allow them, or adding a second suite to an adapter
  # makes its MAPPING suite look unlisted. (It did, immediately.)
  gen=$(echo "$base" | sed -n 's/^test_\(.*\)_mapping\.py$/\1/p')
  [ -n "$gen" ] && grep -qE "^adapter_test +$gen( |\$)" "$0" && continue
  unlisted="$unlisted $base"
done
if [ -n "$unlisted" ]; then
  echo "FAILED: test suite(s) exist but are never executed —$unlisted" >&2
  echo "  add each to a group below. A suite this script does not name is dead weight:" >&2
  echo "  it passes on your machine and guards nothing in CI." >&2
  exit 1
fi

# Service images now run as non-root (uid 1000); the shared uv cache volume is
# root-owned on a fresh machine, so a test container can't initialize it. Hand the
# volume to 1000 once (idempotent) so `uv pip install` works as the image user —
# cheaper and cleaner than running every test container as root.
docker run --rm -u 0:0 -v dida-uv-cache:/uvcache --entrypoint sh dida/base:latest \
  -c 'chown -R 1000:1000 /uvcache' >/dev/null 2>&1 || true

# Every image installs from uv.lock with --frozen, which trusts the lock as written:
# a pyproject edited without `uv lock` would build from the stale lock without a
# word. The lock check refuses that, and ruff is the lint gate the root pyproject
# declares, at the version the lock pins.
echo "== uv lock --check, ruff =="
$RUN dida/base:latest -c "uv lock --check --offline --directory /w \
  && uv export -q --frozen --directory /w --no-emit-workspace --only-group lint -o /tmp/lint.txt \
  && env -u UV_CONSTRAINT -u UV_OVERRIDE uv pip install -q --python /opt/venv/bin/python --target /tmp/tools --require-hashes -r /tmp/lint.txt \
  && /tmp/tools/bin/ruff check --no-cache --quiet ." \
  || { echo "FAILED: uv.lock is stale (run uv lock) or ruff found an error" >&2; exit 1; }

echo "== core + capability + auth + permissions + energy + history + retention (api image) =="
$RUN dida/api:latest -c "$INSTALL; export PYTHONPATH=$PP_API:/w/services/runner/src:/w/adapters/astro/src:/w/adapters/solar/src:/w/adapters/iammeter/src:/w/adapters/peer/src:/w/adapters/landroid/src:/w/adapters/virtual/src:/w/services/planvision/src COVERAGE_FILE=/w/$COVN/.coverage.api; \
  python -m pytest -q --no-header --cov=dida_core --cov=home_core --cov=dida_api \
    --cov=dida_adapter_astro --cov=dida_adapter_iammeter --cov=dida_adapter_peer \
    --cov=dida_adapter_landroid --cov=dida_adapter_solar --cov=dida_runner \
    `# The rest of the shipping fleet, by PATH not by module name: a --cov=<module>` \
    `# for something no test imports contributes NOTHING to the denominator, so` \
    `# these were invisible — 2 543 statements of untested lifecycle sitting` \
    `# outside the number that claims to measure us. A directory makes coverage` \
    `# report them at 0%, which is the honest figure.` \
    --cov=/w/adapters/govee/src/dida_adapter_govee \
    --cov=/w/adapters/frigate/src/dida_adapter_frigate \
    --cov=/w/adapters/homekit/src/dida_adapter_homekit \
    --cov=/w/adapters/broadlink/src/dida_adapter_broadlink \
    --cov=/w/adapters/midea/src/dida_adapter_midea \
    --cov=/w/adapters/virtual/src/dida_adapter_virtual \
    --cov-report= \
    tests/test_core_semantics.py tests/test_capability_contract.py tests/test_capability_registration.py tests/test_auth.py \
    tests/test_permissions.py tests/test_energy.py tests/test_history.py tests/test_replay.py \
    tests/test_radio_tuner.py tests/test_retention.py tests/test_adapter_config.py tests/test_virtual_adapter.py \
    tests/test_failure_gate.py tests/test_geo.py tests/test_ids.py tests/test_media_util.py \
    tests/test_bus_subjects.py tests/test_bus_identity.py tests/test_device_type.py tests/test_health.py tests/test_logbus.py tests/test_logsetup.py \
    tests/test_layering.py \
    tests/test_presence.py tests/test_bus.py tests/test_discovery.py tests/test_adapter_runner.py \
    tests/test_automations_core.py tests/test_alerts.py tests/test_llm_keys.py \
    tests/test_heating_core.py \
    tests/test_help_docs.py \
    tests/test_assistant.py tests/test_astro_mapping.py \
    tests/test_iammeter_mapping.py tests/test_peer_mapping.py \
    tests/test_api_camera_auth.py tests/test_unconfigured_adapters.py tests/test_ws_events.py \
    tests/test_landroid_control.py tests/test_landroid_state.py tests/test_runner_env.py tests/test_host_settings.py \
    tests/test_stats.py tests/test_planvision_errors.py tests/test_upgrade_guard.py \
    tests/test_orphan_references.py tests/test_setting_registry.py tests/test_pool_budget.py tests/test_compose_boundary.py tests/test_broker.py tests/test_broker_scope.py tests/test_camera_urls.py tests/test_adapters_api.py tests/test_camera_access.py tests/test_owntracks_auth.py tests/test_backup_files.py tests/test_assistant_boundary.py tests/test_smartthings_oauth.py tests/test_media_proxies.py tests/test_entry_surface.py tests/test_settings_secrets.py tests/test_push_subscriptions.py tests/test_schedule_routes.py tests/test_history_access.py tests/test_camera_snapshots.py tests/test_migration_scope.py tests/test_pipeline_dac.py tests/test_local_time.py tests/test_reachability.py tests/test_reachability_reassert.py tests/test_people.py \
    core/src/home_core/tests"

# In the JOURNAL image on purpose: the suite imports dida_journal, so running it
# here also proves the shipping image can import its own module — the api image
# would prove only that the working tree parses.
echo "== device-event journal: emit contract + clickhouse sink (journal image) =="
$RUN dida/journal:latest -c "$INSTALL; export PYTHONPATH=$PP_JOURNAL COVERAGE_FILE=/w/$COVN/.coverage.journal; \
  python -m pytest -q --no-header --cov=dida_core --cov=dida_journal --cov-report= \
    tests/test_journal.py"

# The alarm's LAST MILE, in its own image. Everything upstream of this adapter is
# observable (command audit, journal); when it fails, every surface still reads
# healthy and the only symptom is a notification that never came.
echo "== notify adapter — routing + degradation (notify image) =="
$RUN dida/adapter-notify:latest -c "$INSTALL; export PYTHONPATH=$CORE:/w/adapters/notify/src COVERAGE_FILE=/w/$COVN/.coverage.notify; \
  python -m pytest -q --no-header --cov=dida_adapter_notify --cov-report= \
    tests/test_notify_adapter.py"

echo "== automation transitions + starlark sandbox (automation image) =="
$RUN dida/automation:latest -c "$INSTALL; export PYTHONPATH=$PP_AUTO COVERAGE_FILE=/w/$COVN/.coverage.auto; \
  python -m pytest -q --no-header --cov=dida_core --cov=dida_automation --cov-report= \
    tests/test_automation_transitions.py tests/test_sandbox.py tests/test_starlark_worker.py \
    tests/test_computed_helper.py tests/test_heating_controller.py tests/test_automation_engine.py \
    tests/test_manual_override.py"

# --- integration: the engine SERVICE + engine projection + auth HTTP surface,
#     against an EPHEMERAL Postgres and NATS the runner spins up. One throwaway pg
#     shared by all (disjoint tables: engine → current_state, api → users); NATS is
#     there so `main()` can be booted for real — the 160 lines that wire the single
#     writer of all state together were otherwise the largest untested block in the
#     project. Images guaranteed by the preflight. ---
echo "== integration: engine service + projection + auth HTTP (ephemeral postgres + nats + clickhouse) =="
# A run killed by a signal (Ctrl-C, a timeout that kills the parent) never reaches
# the EXIT trap, and every leaked stack holds a docker network. The daemon's default
# address pool has ~31 of them, after which `docker network create` below fails for
# EVERY later run — including a pre-push gate that has nothing to do with the leak.
# Reap the stacks whose owning PID is gone; a concurrent gate's PID is alive and is
# left alone.
for orphan in $(docker ps -a --filter 'name=^dida-test-' --format '{{.Names}}' 2>/dev/null) \
              $(docker network ls --filter 'name=^dida-test-net-' --format '{{.Name}}' 2>/dev/null); do
  opid=${orphan##*-}
  [ "$opid" = "$$" ] && continue
  kill -0 "$opid" 2>/dev/null && continue
  case $orphan in
    dida-test-net-*) docker network rm "$orphan" >/dev/null 2>&1 || true ;;
    *) docker rm -f "$orphan" >/dev/null 2>&1 || true ;;
  esac
done

NET="dida-test-net-$$"; PGC="dida-test-pg-$$"; NATSC="dida-test-nats-$$"; CHC="dida-test-ch-$$"; ACLC="dida-test-natsacl-$$"
KEYS="dida-test-keys-$$"; SECRET="test-fernet-secret-key-for-integration"
PGENV="-e POSTGRES_HOST=$PGC -e POSTGRES_PORT=5432 -e POSTGRES_USER=dida -e POSTGRES_PASSWORD=test -e POSTGRES_DB=dida"
APPENV="-e POSTGRES_HOST=$PGC -e POSTGRES_PORT=5432 -e POSTGRES_USER=dida_app -e POSTGRES_DB=dida \
  -e POSTGRES_PASSWORD_FILE=/keys/db-dida_app/password -v $KEYS:/keys:ro"
CHENV="-e CLICKHOUSE_HOST=$CHC -e CLICKHOUSE_PASSWORD=test"
cleanup() {
  docker rm -f "$PGC" "$NATSC" "$CHC" "$ACLC" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true; docker volume rm "$KEYS" >/dev/null 2>&1 || true
}
docker network create "$NET" >/dev/null
docker run -d --name "$PGC" --network "$NET" \
  -e POSTGRES_USER=dida -e POSTGRES_PASSWORD=test -e POSTGRES_DB=dida \
  pgvector/pgvector:pg18 >/dev/null
# JetStream: the engine's state/entity/command consumers are durable, so a plain
# core-NATS server would fail at `ensure_streams` and the service would never boot.
docker run -d --name "$NATSC" --network "$NET" nats:2.14-alpine -js >/dev/null
docker run -d --name "$CHC" --network "$NET" \
  -e CLICKHOUSE_USER=dida -e CLICKHOUSE_PASSWORD=test -e CLICKHOUSE_DB=dida -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
  clickhouse/clickhouse-server:25.8 >/dev/null
ready=0
# `pg_isready` answers YES during initdb, when the bootstrap server is up but the
# application database does not exist yet — a race that surfaces as a flaky
# InvalidCatalogNameError. Ask for the actual database instead.
for _ in $(seq 1 60); do
  if docker exec "$PGC" psql -U dida -d dida -tAc 'SELECT 1' >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.4
done
[ "$ready" = 1 ] || { echo "  FAILED: ephemeral postgres never became ready"; exit 1; }
nats_ready=0
for _ in $(seq 1 60); do
  # `docker logs` rather than a port probe: the port opens before JetStream has
  # finished initialising its store, and a stream created in that window fails.
  if docker logs "$NATSC" 2>&1 | grep -q "Server is ready"; then nats_ready=1; break; fi
  sleep 0.4
done
[ "$nats_ready" = 1 ] || { echo "  FAILED: ephemeral nats never became ready"; exit 1; }
ch_ready=0
for _ in $(seq 1 100); do
  if docker exec "$CHC" clickhouse-client --user dida --password test -q 'SELECT 1' >/dev/null 2>&1; then ch_ready=1; break; fi
  sleep 0.3
done
[ "$ch_ready" = 1 ] || { echo "  FAILED: ephemeral clickhouse never became ready"; exit 1; }
# The services log in as the house's do: the keys service writes the passwords and
# provisions the roles on the empty database, then every suite below that stands in
# for the engine or the api connects as dida_app, not as the superuser.
docker run --rm --network "$NET" $PGENV -u 0:0 -e DIDA_SECRET_KEY="$SECRET" -v "$KEYS:/keys" \
  -v "$ROOT:/w" -w /w --entrypoint sh dida/api:latest \
  -c "export PYTHONPATH=$PP_API; python -m dida_core.identity" \
  || { echo "FAILED: the keys service could not provision the database roles" >&2; exit 1; }
docker run --rm --network "$NET" $APPENV $CHENV -e UV_CACHE_DIR=/uvcache -v dida-uv-cache:/uvcache \
  -e DIDA_NATS_URL="nats://$NATSC:4222" -e DIDA_MIGRATIONS_DIR=/w/db/migrations \
  -v "$ROOT:/w" -w /w --entrypoint sh dida/engine:latest \
  -c "$INSTALL; export PYTHONPATH=$PP_ENGINE COVERAGE_FILE=/w/$COVN/.coverage.eng; \
      python -m pytest -q --no-header --cov=dida_engine --cov-report= \
        tests/test_engine_service.py tests/test_engine_projection.py tests/test_engine_lifecycle.py tests/test_history_writer.py tests/test_history_schema_fault.py \
        tests/test_device_rename.py tests/test_engine_reachability.py tests/test_day_rollup.py"
docker run --rm --network "$NET" $APPENV -e UV_CACHE_DIR=/uvcache -v dida-uv-cache:/uvcache \
  -e DIDA_SECRET_KEY="$SECRET" \
  -v "$ROOT:/w" -w /w --entrypoint sh dida/api:latest \
  -c "$INSTALL; export PYTHONPATH=$PP_API COVERAGE_FILE=/w/$COVN/.coverage.apiint; \
      python -m pytest -q --no-header --cov=dida_api --cov-report= \
        tests/test_api_auth.py tests/test_api_command_permissions.py tests/test_api_users.py \
        tests/test_api_zones.py tests/test_api_areas.py tests/test_api_floors.py \
        tests/test_api_devices.py tests/test_api_settings.py tests/test_api_scenes.py \
        tests/test_api_automations.py tests/test_api_retention.py tests/test_api_adapters.py \
        tests/test_api_owntracks.py tests/test_api_virtual.py tests/test_api_computed.py \
        tests/test_api_entry.py tests/test_api_system.py tests/test_api_entities.py \
        tests/test_api_backup.py tests/test_api_floors_more.py tests/test_api_energy_more.py \
        tests/test_api_owntracks_more.py tests/test_api_users_more.py tests/test_api_alerts.py \
        tests/test_api_boundary_more.py tests/test_api_mobile.py tests/test_api_photos.py \
        tests/test_api_translations.py tests/test_onboarding_flow.py \
        tests/test_api_panel.py tests/test_api_assistant.py tests/test_api_heating.py \
        tests/test_api_contacts.py tests/test_api_upkeep.py tests/test_api_broker.py"
# The roles themselves, in their own database from the superuser's seat: the upgrade
# a house takes (tables the superuser owns, handed over, the new migration applied by
# the app role) and what each role is refused.
echo "== database roles: hand-over and boundaries (own database) =="
docker exec "$PGC" psql -U dida -d dida -q -c 'CREATE DATABASE didaroles' >/dev/null
docker run --rm --network "$NET" $PGENV -e POSTGRES_DB=didaroles -e DIDA_SECRET_KEY="$SECRET" \
  -e UV_CACHE_DIR=/uvcache -v dida-uv-cache:/uvcache \
  -v "$ROOT:/w" -w /w --entrypoint sh dida/api:latest \
  -c "$INSTALL; export PYTHONPATH=$PP_API COVERAGE_FILE=/w/$COVN/.coverage.roles; \
      python -m pytest -q --no-header --cov=dida_core --cov-report= tests/test_db_roles.py"
# Every bus identity, tried from an adapter image against the server the house runs:
# docker/nats.conf and nats-run.sh as shipped, with the logins the keys service above
# wrote from the throwaway secret.
echo "== bus boundary: every identity against docker/nats.conf =="
docker run -d --name "$ACLC" --network "$NET" \
  -v "$ROOT/docker/nats.conf:/etc/nats/nats.conf:ro" -v "$ROOT/docker/nats-run.sh:/etc/nats/nats-run.sh:ro" \
  --mount "type=volume,src=$KEYS,dst=/etc/nats/auth,volume-subpath=nats-server,readonly" \
  nats:2.14-alpine sh /etc/nats/nats-run.sh >/dev/null
acl_ready=0
for _ in $(seq 1 60); do
  if docker logs "$ACLC" 2>&1 | grep -q "Server is ready"; then acl_ready=1; break; fi
  sleep 0.4
done
[ "$acl_ready" = 1 ] || { echo "  FAILED: nats with docker/nats.conf never became ready"; docker logs "$ACLC" 2>&1 | tail -5; exit 1; }
docker run --rm --network "$NET" -e DIDA_ACL_NATS="$ACLC:4222" -e DIDA_ACL_KEYS=/keys -v "$KEYS:/keys:ro" \
  -e UV_CACHE_DIR=/uvcache -v dida-uv-cache:/uvcache \
  -v "$ROOT:/w" -w /w --entrypoint sh dida/adapter-mqtt:latest \
  -c "$INSTALL; python -m pytest -q --no-header -p no:cacheprovider tests/test_bus_boundary.py"

# Migration integrity + the UPGRADE path, in their OWN database: this suite drops
# and rebuilds `public` and rewrites schema_migrations, which every other suite
# depends on. Everything else here tests a FRESH install (empty DB, apply all);
# this is the only place an OLD populated database meets today's code.
echo "== migration integrity + upgrade path (own database) =="
docker exec "$PGC" psql -U dida -d dida -q -c 'DROP DATABASE IF EXISTS didamig' >/dev/null
docker exec "$PGC" psql -U dida -d dida -q -c 'CREATE DATABASE didamig' >/dev/null
docker run --rm --network "$NET" $PGENV -e POSTGRES_DB=didamig \
  -e UV_CACHE_DIR=/uvcache -v dida-uv-cache:/uvcache \
  -v "$ROOT:/w" -w /w --entrypoint sh dida/api:latest \
  -c "$INSTALL; export PYTHONPATH=$PP_API COVERAGE_FILE=/w/$COVN/.coverage.mig; \
      python -m pytest -q --no-header --cov=dida_core --cov-report= tests/test_migration_integrity.py"

# The live dev database against the schema the migrations build. The demo refresh
# once ran DDL on dev and dropped ON UPDATE CASCADE from a foreign key; nothing
# compared the two, so dev ran on a schema no migration describes. CI has no live
# database to compare.
echo "== schema drift (live dev database vs migrations) =="
if docker inspect dida-postgres >/dev/null 2>&1; then
  DRIFT=$(mktemp -d)
  ls "$ROOT/db/migrations" | grep '\.sql$' | sort > "$DRIFT/repo"
  docker exec dida-postgres psql -U dida -d dida -tAc 'SELECT version FROM schema_migrations' | sort > "$DRIFT/live"
  unapplied=$(comm -23 "$DRIFT/repo" "$DRIFT/live" | tr '\n' ' ')
  if [ -n "$unapplied" ]; then
    rm -rf "$DRIFT"
    echo "FAILED: the dev database has not applied ${unapplied}— restart the api, then push" >&2
    exit 1
  fi
  docker exec "$PGC" psql -U dida -d dida -q -c 'CREATE DATABASE didaschema' >/dev/null
  docker run --rm --network "$NET" $PGENV -e POSTGRES_DB=didaschema \
    -e UV_CACHE_DIR=/uvcache -v dida-uv-cache:/uvcache \
    -v "$ROOT:/w" -w /w --entrypoint sh dida/api:latest \
    -c "$INSTALL; export PYTHONPATH=$PP_API; python tests/apply_migrations.py"
  schema() {
    docker exec "$1" pg_dump -U dida -d "$2" --schema-only --no-owner --no-privileges > "$3.raw"
    grep -vE '^(--|SET |SELECT pg_catalog\.set_config|\\(un)?restrict )' "$3.raw" | sed '/^$/d' > "$3"
  }
  schema "$PGC" didaschema "$DRIFT/migrations"
  schema dida-postgres dida "$DRIFT/dev"
  if ! diff "$DRIFT/migrations" "$DRIFT/dev" >&2; then
    rm -rf "$DRIFT"
    echo "FAILED: the dev database differs from what the migrations build (< migrations, > dev)" >&2
    exit 1
  fi
  rm -rf "$DRIFT"
else
  echo "  no live dev database on this host"
fi

cleanup
cleanup() { :; }

# Adapter native<->canonical mapping — each in its own adapter image (guaranteed by
# the preflight, so a missing one already failed loud above, never a silent skip).
echo "== adapter mapping (per adapter image) =="
adapter_test() {
  # Every extra file is a second suite for the same adapter (mapping + lifecycle);
  # they share one image and one coverage file, so they run in one pass.
  local a=$1; shift
  $RUN "dida/adapter-$a:latest" -c "$INSTALL; export PYTHONPATH=$CORE:/w/adapters/$a/src COVERAGE_FILE=/w/$COVN/.coverage.$a; \
    python -m pytest -q --no-header --cov=dida_adapter_$a --cov-report= tests/test_${a}_mapping.py $*"
}
adapter_test broadlink
adapter_test homekit tests/test_homekit_lifecycle.py
adapter_test frigate
adapter_test govee
adapter_test announce
adapter_test ecowitt
adapter_test mqtt tests/test_mqtt_lifecycle.py tests/test_zigbee_onboarding.py
adapter_test shelly
adapter_test esphome tests/test_esphome_lifecycle.py
adapter_test tuya tests/test_tuya_lifecycle.py
adapter_test smartthings tests/test_smartthings_reachability.py
adapter_test contacts
adapter_test androidtv tests/test_androidtv_lifecycle.py
adapter_test cast
adapter_test dlna tests/test_dlna_lifecycle.py
adapter_test harmony
adapter_test heos
adapter_test volumio tests/test_volumio_lifecycle.py
adapter_test roidmi
adapter_test dreame
adapter_test samsungtv
adapter_test opus
adapter_test baba tests/test_baba_light.py
adapter_test panasonic
# unifi maps people to controller CLIENTS, not to device state, so its suite is
# test_unifi_presence.py rather than the _mapping.py the helper assumes. A person
# matched to the wrong client reports them home while they are away.
$RUN dida/adapter-unifi:latest -c "$INSTALL; export PYTHONPATH=$CORE:/w/adapters/unifi/src COVERAGE_FILE=/w/$COVN/.coverage.unifi; \
  python -m pytest -q --no-header --cov=dida_adapter_unifi --cov-report= tests/test_unifi_presence.py"

# denon carries its testable logic in protocol.py (Telnet parsing + volume math), so
# its test is test_denon_protocol.py, not the _mapping.py the helper assumes. Coverage
# is scoped to the .protocol MODULE (not the whole package like the mapping adapters):
# denon's adapter.py is a large Telnet-I/O lifecycle no unit test exercises, and
# folding its ~200 uncovered statements into the shared floor is a deliberate,
# MEASURED re-baseline — not something to bolt on with this fix. The pure protocol
# module is what the test covers, and gating it is what protects this bug from
# regressing.
$RUN dida/adapter-denon:latest -c "$INSTALL; export PYTHONPATH=$CORE:/w/adapters/denon/src COVERAGE_FILE=/w/$COVN/.coverage.denon; \
  python -m pytest -q --no-header --cov=dida_adapter_denon.protocol --cov-report= tests/test_denon_protocol.py"

# cloudflare parses and rewrites whichever ingress the installation carries, so its
# tests are named for the two files rather than the _mapping.py the helper assumes —
# there is no device state to map, only a file whose round-trip must not move a route.
$RUN dida/adapter-cloudflare:latest -c "$INSTALL; export PYTHONPATH=$CORE:/w/adapters/cloudflare/src COVERAGE_FILE=/w/$COVN/.coverage.cloudflare; \
  python -m pytest -q --no-header --cov=dida_adapter_cloudflare --cov-report= tests/test_cloudflare_source.py tests/test_cloudflare_tunnel.py"

# calendar's recurrence math (test_calendar.py) — its own image: the suite imports
# dida_adapter_calendar, which the api image does not carry.
$RUN dida/adapter-calendar:latest -c "$INSTALL; export PYTHONPATH=$CORE:/w/adapters/calendar/src COVERAGE_FILE=/w/$COVN/.coverage.calendar; \
  python -m pytest -q --no-header --cov=dida_adapter_calendar --cov-report= tests/test_calendar.py"

# netmgr's VLAN reconcile primitives — its own image (dida_netmgr isn't in the api
# image). The placeholder-vs-lease distinction and the dhclient host-name escaping
# are the two places a mistake either strands DIDA off the IoT VLAN or writes
# attacker-shaped text into a dhclient config. The image runs as root, so bytecode
# it wrote into the mounted source would be a directory its owner cannot delete.
$RUN -e PYTHONDONTWRITEBYTECODE=1 dida/netmgr:latest -c "$INSTALL; export PYTHONPATH=$CORE:/w/services/netmgr/src COVERAGE_FILE=/w/$COVN/.coverage.netmgr; \
  python -m pytest -q --no-header --cov=dida_netmgr --cov-report= tests/test_netmgr.py tests/test_netmgr_journal.py"

# planvision's CV pipeline (test_planvision.py) — needs opencv + numpy, which live
# only in its own image.
$RUN dida/planvision:latest -c "$INSTALL; export PYTHONPATH=$CORE:/w/services/planvision/src COVERAGE_FILE=/w/$COVN/.coverage.planvision; \
  python -m pytest -q --no-header --cov=dida_planvision --cov-report= tests/test_planvision.py tests/test_planvision_logging.py"

# --- UI (vitest) — the frontend's first behavioural tests. Runs in a plain node
# image against the working tree: node_modules is gitignored, so a fresh clone
# installs once (npm ci) and every later run reuses it. NOT folded into the Python
# coverage number — a separate language with its own runner; the point is that the
# store's optimistic-revert and replay paths stop being untested, not the %.
# As the invoking user: node_modules is in the working tree, and root-owned files
# there made the next `npm install` on the host fail. Re-installed whenever the
# lockfile is newer than the tree npm last wrote, so a dependency bump is tested
# against what it bumped to, not against the tree left from before it.
NODE_AS_ME="--user $(id -u):$(id -g) -e HOME=/tmp"
NPM_FRESH='[ node_modules/.package-lock.json -nt package-lock.json ] || npm ci --no-audit --no-fund >/dev/null 2>&1'
# --- matter-bridge (vitest) — the DIDA<->Matter conversions. A bug here moves a
# real device the wrong way on a voice command with no DIDA-side symptom, so the
# mapping is split out of index.ts (which opens PG/NATS/Matter at import) and
# tested on its own. Same self-healing install as the ui group. tsx runs the bridge
# without checking a single type, so tsc here is the only place a wrong matter.js
# call shows before a controller trips over it.
echo ""
echo "== matter-bridge (vitest + tsc) =="
docker run --rm $NODE_AS_ME -v "$ROOT/services/matter-bridge:/w" -w /w node:24-slim sh -c \
  "$NPM_FRESH; npx vitest run && npx tsc -p tsconfig.json" \
  || { echo "FAILED: matter-bridge suite" >&2; exit 1; }

echo ""
echo "== ui (vitest) =="
docker run --rm $NODE_AS_ME -v "$ROOT/ui:/w" -w /w node:24-slim sh -c \
  "$NPM_FRESH; npx svelte-kit sync && npx vitest run" \
  || { echo "FAILED: ui suite" >&2; exit 1; }

# --- UI (svelte-check) — the types the vitest suite cannot see. A call to an API
# client method that does not exist compiles, bundles, ships, and only then throws
# in the browser: `api.settings()` for `api.getSettings()` put a red banner across
# the Adapters page on production, found by the person using it. The tree checks
# clean, so this is a floor to hold rather than a backlog to burn down.
echo ""
echo "== ui (svelte-check) =="
docker run --rm $NODE_AS_ME -v "$ROOT/ui:/w" -w /w node:24-slim sh -c \
  "$NPM_FRESH; npx svelte-kit sync && npx svelte-check --tsconfig ./tsconfig.json --output human" \
  || { echo "FAILED: ui types" >&2; exit 1; }

# --- UI (words) — a key built at runtime compiles whatever it names. Six kinds of
# device-timeline event read as their raw keys for weeks because nothing held the
# journal's vocabulary to the catalogue; this reads the backend's vocabularies too.
echo ""
echo "== ui (words) =="
docker run --rm $NODE_AS_ME -v "$ROOT:/repo:ro" -w /repo node:24-slim node ui/src/lib/i18n/words.mjs \
  || { echo "FAILED: ui words" >&2; exit 1; }

# --- UI (names) — the kit's rule for every product on it: a button, link or clicked
# element has a name a screen reader can say, and a sign on its face (✕ ↻ −) is not one.
echo ""
echo "== ui (names) =="
docker run --rm $NODE_AS_ME -v "$ROOT:/repo:ro" -w /repo node:24-slim node ui/src/lib/kit/names.mjs ui/src \
  || { echo "FAILED: ui names" >&2; exit 1; }

# --- UI (type) — the kit's seven steps are Tailwind's text scale (app.css). A size
# written out below 2rem is one an eye cannot tell from its neighbour, and each one
# invites the next; above the top step is a glyph fitted to its box, not text.
echo ""
echo "== ui (type) =="
docker run --rm $NODE_AS_ME -v "$ROOT:/repo:ro" -w /repo node:24-slim node ui/src/lib/kit/type.mjs ui/src \
  || { echo "FAILED: ui type" >&2; exit 1; }

# --- UI (kit) — the kit's own checks, tested where they run: in this product's gate.
echo ""
echo "== ui (kit checks) =="
docker run --rm $NODE_AS_ME -v "$ROOT/ui:/w:ro" -w /w node:24-slim sh -c 'node --test --test-reporter=dot src/lib/kit/*.test.mjs' \
  || { echo "FAILED: ui kit checks" >&2; exit 1; }

echo ""
echo "== combined coverage (floor $COV_MIN%) =="
$RUN dida/api:latest -c "uv pip install --python /opt/venv/bin/python -q coverage >/dev/null 2>&1; \
  cd /w && export COVERAGE_FILE=/w/$COVN/.coverage && coverage combine && \
  coverage report --precision=1 --sort=cover --fail-under=$COV_MIN"

echo ""
echo "ALL SUITES PASSED"

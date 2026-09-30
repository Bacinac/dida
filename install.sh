#!/usr/bin/env bash
# DIDA installer — one command from a fresh clone to a running instance.
#
#   ./install.sh              build everything, start the stack, wait for health
#   ./install.sh --no-start   only prepare .env + validate compose (no build/up)
#   ./install.sh --help
#
# Idempotent: a re-run KEEPS existing secrets and any value you set, fills only
# what is missing, then rebuilds and waits until the whole stack is healthy.
# Per-adapter config (brokers, device hosts, credentials) is NOT here — you add
# it in the running UI under Settings → Adapters.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=${DIDA_ENV_FILE:-.env}   # overridable so the prep step can be tested in isolation
EXAMPLE=.env.example
START=1
for a in "$@"; do
  case "$a" in
    --no-start) START=0 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

# ── helpers ────────────────────────────────────────────────────────────────
gen() { openssl rand -hex "${1:-24}" 2>/dev/null \
        || python3 -c 'import secrets,sys; print(secrets.token_hex(int(sys.argv[1])))' "${1:-24}"; }

env_read() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'; }

# Set KEY=VALUE, replacing in place (even when the current value is empty) or
# appending. Python does the edit so values with / & etc. need no escaping.
env_set() {
  python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().splitlines(True)
done = False
for i, l in enumerate(lines):
    if l.split("=", 1)[0] == key and not done:
        lines[i] = f"{key}={val}\n"; done = True
if not done:
    if lines and not lines[-1].endswith("\n"): lines[-1] += "\n"
    lines.append(f"{key}={val}\n")
open(path, "w").writelines(lines)
PY
}

# ── 0. the kit and home-core — submodules a plain `git clone` leaves empty ───
git submodule update --init --quiet

# ── 1. .env from the template, then merge in any settings added upstream ─────
if [ ! -f "$ENV_FILE" ]; then
  cp "$EXAMPLE" "$ENV_FILE"; echo "created $ENV_FILE from $EXAMPLE"
fi
while IFS= read -r line; do
  case "$line" in
    [A-Z]*=*) k=${line%%=*}; grep -qE "^$k=" "$ENV_FILE" || {
      printf '%s\n' "$line" >> "$ENV_FILE"; echo "added new setting: $k"; } ;;
  esac
done < "$EXAMPLE"

# ── 2. secrets — generated once, kept on every re-run ────────────────────────
for spec in POSTGRES_PASSWORD:24 CLICKHOUSE_PASSWORD:24 DIDA_SECRET_KEY:32 DIDA_MQTT_PASSWORD:24; do
  key=${spec%%:*}; len=${spec##*:}
  [ -z "$(env_read "$key")" ] && { env_set "$key" "$(gen "$len")"; echo "generated $key"; }
done
ADMIN_USER=$(env_read DIDA_ADMIN_USERNAME); ADMIN_USER=${ADMIN_USER:-admin}
ADMIN_PW=$(env_read DIDA_ADMIN_PASSWORD)
if [ -z "$ADMIN_PW" ]; then ADMIN_PW=$(gen 9); env_set DIDA_ADMIN_PASSWORD "$ADMIN_PW"; echo "generated DIDA_ADMIN_PASSWORD"; fi

# ── 3. storage paths — absolute, derived from this clone ─────────────────────
# Docker's local volume driver resolves `device:` against / , not the project
# dir, so these cannot be left relative.
REPO=$(pwd -P)
# The runner drives compose from inside a container and must see this clone at the
# path the host knows it by — compose derives every bind mount's host path from it.
[ -z "$(env_read DIDA_REPO_HOST)" ]   && { env_set DIDA_REPO_HOST "$REPO";           echo "repo   → $REPO"; }
[ -z "$(env_read DIDA_STATE_HOST)" ]  && { env_set DIDA_STATE_HOST "$REPO/state";    echo "state  → $REPO/state"; }
[ -z "$(env_read DIDA_BACKUP_HOST)" ] && { env_set DIDA_BACKUP_HOST "$REPO/backups"; echo "backups → $REPO/backups"; }
mkdir -p "$(env_read DIDA_STATE_HOST)" "$(env_read DIDA_BACKUP_HOST)"
# ── 4. host-specific bits (best-effort; verify on a multi-homed host) ────────
# DIDA_LAN_IP is MANDATORY (compose refuses to start without it) and is the one
# canonical "where DIDA is on the LAN" — AV adapters bind SSDP to it.
if [ -z "$(env_read DIDA_LAN_IP)" ]; then
  ip=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -1)
  if [ -n "$ip" ]; then
    env_set DIDA_LAN_IP "$ip"
    echo "detected LAN address → DIDA_LAN_IP=$ip (verify if this host is multi-homed)"
  else
    echo "WARNING: could not detect a LAN IP — set DIDA_LAN_IP in $ENV_FILE by hand" >&2
  fi
fi
# The group owning the Docker socket, so the docker-proxy-* containers can read it.
gid=$(stat -c %g /var/run/docker.sock 2>/dev/null) || { echo "no /var/run/docker.sock — is Docker installed?" >&2; exit 1; }
env_set DIDA_DOCKER_GID "$gid"
# Replace ONLY the template's placeholder NIC, never a value you chose.
nic=$(ip route show default 2>/dev/null | sed -n 's/.*dev \([^ ]*\).*/\1/p' | head -1)
if [ -n "$nic" ]; then
  [ "$(env_read DIDA_NET_PARENT)" = "ens18" ] && { env_set DIDA_NET_PARENT "$nic"; echo "detected NIC → DIDA_NET_PARENT=$nic"; }
  [ "$(env_read DIDA_MATTER_INTERFACE)" = "ens18" ] && env_set DIDA_MATTER_INTERFACE "$nic"
fi

if [ "$START" = 0 ]; then
  echo "validating compose config…"
  docker compose --env-file "$ENV_FILE" config -q && echo "OK — $ENV_FILE prepared, compose valid (not started)"
  exit 0
fi

# ── 5. mosquitto broker credential (gitignored) from DIDA_MQTT_* ─────────────
MQTT_USER=$(env_read DIDA_MQTT_USERNAME); MQTT_USER=${MQTT_USER:-dida}
MQTT_PW=$(env_read DIDA_MQTT_PASSWORD)
# Rendered from DIDA_MQTT_PASSWORD every run, not just when missing: a stale file
# from an earlier password silently makes the broker reject every client while
# each container still reports healthy.
if [ -n "$MQTT_PW" ]; then
  # `mosquitto_passwd -c` refuses to write to an existing path (incl. /dev/stdout on
  # modern builds), so hash into a fresh temp file inside the container, then cat it.
  docker run --rm eclipse-mosquitto:2 sh -c \
    'mosquitto_passwd -b -c /tmp/p "$1" "$2" >/dev/null 2>&1 && cat /tmp/p' _ "$MQTT_USER" "$MQTT_PW" \
    > docker/mosquitto.passwd
  echo "wrote docker/mosquitto.passwd"
fi

# ── 6. build + start ─────────────────────────────────────────────────────────
# revision.json is git-derived, gitignored, and bind-mounted read-only into the
# api. Nothing on a fresh clone creates it — and docker materialises a missing
# bind source as a DIRECTORY, which then makes every later deploy die in
# gen-revision. install-hooks writes it and activates the hooks that keep it
# current on every later commit/merge.
./scripts/install-hooks.sh >/dev/null
echo "building base image…";  docker compose --profile bases build base
echo "building services…";    docker compose --profile "*" build
# A service that refuses to start (unreachable mount, taken port) must not abort
# the run with a raw docker error — step 7 names the container and where to look.
echo "starting the stack…";   docker compose up -d --remove-orphans 2>&1 | tee /tmp/dida-up.log || true

# Non-root services (uid 1000) own their /state dirs. On a FRESH install the
# per-service dirs are created root-owned by the first `up`, so chown them now and
# let restart: unless-stopped pick them up (idempotent; mirrors deploy/prod.sh).
STATE_HOST=$(env_read DIDA_STATE_HOST)
if [ -n "$STATE_HOST" ]; then
  for d in "$STATE_HOST"/adapter-* "$STATE_HOST"/api "$STATE_HOST"/automation \
           "$STATE_HOST"/engine "$STATE_HOST"/matter-bridge "$STATE_HOST"/ingress \
           "$STATE_HOST"/cloudflared; do
    [ -d "$d" ] && chown -R 1000:1000 "$d" 2>/dev/null || true
  done
  docker compose up -d --remove-orphans >/dev/null 2>&1 || true
fi
# The broker reads its password file only at startup, and `up -d` leaves an
# already-running container alone — so a re-rendered file needs this to take.
docker compose restart mosquitto >/dev/null 2>&1 || true

# ── 7. wait until every container is healthy (initdb + migrations + adapters) ─
echo "waiting for health…"
bad=""
for _ in $(seq 1 30); do
  sleep 10
  bad=$(docker compose ps -a --format '{{.Name}} {{.Status}}' \
        | grep -vE '^dida-keys Exited \(0\)' \
        | grep -E 'unhealthy|health: starting|Exited|Restarting|Created|Dead' || true)
  [ -z "$bad" ] && break
done
if [ -n "$bad" ]; then
  echo "NOT HEALTHY after ~5 min:"; echo "$bad"
  # A container that never left Created has no logs — the reason is the start
  # error (unreachable mount, port in use), which only appears in the up output.
  grep -iE '^error|error response from daemon' /tmp/dida-up.log 2>/dev/null || true
  echo "logs: docker compose logs --tail 50 <service>"; exit 1
fi

# ── 8. prove the broker actually accepts the credentials ─────────────────────
# Container health only says the process is alive: every MQTT client can be
# rejected while the whole stack still reports healthy, so assert it for real.
if ! docker compose exec -T mosquitto \
     mosquitto_pub -h 127.0.0.1 -u "$MQTT_USER" -P "$MQTT_PW" -t dida/install-check -m ok >/dev/null 2>&1; then
  echo "MQTT broker rejected $MQTT_USER — docker/mosquitto.passwd does not match DIDA_MQTT_PASSWORD"
  echo "logs: docker compose logs --tail 20 mosquitto"; exit 1
fi

UI_PORT=$(env_read DIDA_UI_PORT); UI_PORT=${UI_PORT:-5273}
cat <<EOF

  ✓ DIDA is up — $(docker compose ps -aq | wc -l) containers healthy.

    UI      http://localhost:$UI_PORT
    Admin   $ADMIN_USER / $ADMIN_PW

  Next: open the UI, then add your devices under Settings → Adapters.
  Secrets live in $ENV_FILE (gitignored). Re-run ./install.sh anytime; it keeps them.
EOF

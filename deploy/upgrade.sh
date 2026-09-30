#!/usr/bin/env bash
# Upgrade a RUNNING DIDA installation in place. Run it ON the host, from the
# repo root:
#
#     ./deploy/upgrade.sh
#
# It is the whole upgrade: dump the database, remember the running images, fetch,
# rebuild base then services, restart, wait for health, and put the previous stack
# back if health does not come. Nothing here is specific to one installation.
#
# `deploy/prod.sh` ships THIS file to the host and runs it — the operator upgrade
# and the maintainer deploy are the same code, so a path that works for one cannot
# quietly rot for the other.
#
# What it will NOT do: silently continue past a failed backup, restore old images
# on top of a schema a destructive migration has already changed, or start without
# a way back. Each of those exits non-zero and says what to do.
set -euo pipefail

# Normally the repo is this script's parent. `prod.sh` ships this file to /tmp on a
# remote host and runs it from there, where that inference is wrong — it sets
# DIDA_REPO instead. Shipping the file (rather than streaming it over `ssh bash -s`)
# is deliberate; see prod.sh.
ROOT=${DIDA_REPO:-$(cd "$(dirname "$0")/.." && pwd)}
cd "$ROOT"

# Everything the compose file needs lives here; the upgrade reads it for the DB
# credentials and the state/backup locations.
[ -f .env ] || { echo "FAILED: no .env in $ROOT — this is not an installed DIDA" >&2; exit 1; }
env_read() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'; }

PG_USER=$(env_read POSTGRES_USER); PG_USER=${PG_USER:-dida}
PG_DB=$(env_read POSTGRES_DB);     PG_DB=${PG_DB:-dida}
STATE_HOST=$(env_read DIDA_STATE_HOST)
BACKUP_HOST=$(env_read DIDA_BACKUP_HOST)
KEEP=${DIDA_UPGRADE_KEEP:-5}       # how many pre-upgrade dumps to retain

# ── 1. backup, BEFORE anything can change ────────────────────────────────────
# The single thing that turns a bad upgrade from a disaster into an inconvenience.
# It runs first — before the fetch, so it captures the schema the CURRENT code was
# written against — and a failure here stops the upgrade rather than being logged
# and stepped over.
#
# pg_dump comes from the postgres container itself, so the client always matches
# the server; the api image carries pg_dump too, but during an upgrade the api is
# exactly the thing being replaced.
if ! docker compose ps --status running --format '{{.Service}}' | grep -qx postgres; then
  echo "FAILED: postgres is not running — nothing to upgrade (use install.sh for a new install)" >&2
  exit 1
fi

BACKUP_DIR="${BACKUP_HOST:-$ROOT/.backups}/pre-upgrade"
mkdir -p "$BACKUP_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DUMP="$BACKUP_DIR/dida-$STAMP.dump"

echo "== backing up the database to $DUMP =="
if ! docker compose exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" \
       -Fc --no-owner --no-acl > "$DUMP"; then
  rm -f "$DUMP"
  echo "FAILED: pg_dump did not complete — refusing to upgrade without a backup" >&2
  exit 1
fi
# A file of the right shape is not proof it is restorable, so the archive is
# actually READ — every data block decompressed — with the SQL thrown away. The
# obvious cheaper check, `pg_restore --list`, is not enough: a custom-format
# archive keeps its table of contents near the FRONT, so a dump truncated halfway
# through the data still lists perfectly. Measured: a 50%-truncated dump passes
# `--list` (rc=0) and fails this (rc=1). A full disk or a killed container
# produces exactly that dump, and it would have been called a good backup.
# Cost of reading it all: 180 ms for 2.4 MB.
#
# No filename, so pg_restore reads stdin. Naming `/dev/stdin` looks equivalent and
# is not — pg_restore reopens the path and cannot seek it, so it fails with "did
# not find magic string in file header" on a PERFECTLY GOOD dump, which would have
# aborted every upgrade here. Both measured against the running database.
if ! docker compose exec -T postgres pg_restore -f /dev/null < "$DUMP" >/dev/null 2>&1; then
  echo "FAILED: the dump is not a readable pg_dump archive — refusing to upgrade" >&2
  echo "        (kept at $DUMP for inspection)" >&2
  exit 1
fi
# `ls -lh`, not `du -h`: du reports ALLOCATED BLOCKS, and on the NFS share the
# backups live on a freshly written 3 MB dump reports as "512" — a healthy run
# whose log reads like a 512-byte backup. A line that looks like a failure when
# nothing is wrong is worse than no line.
echo "backup OK — $(ls -lh "$DUMP" | awk '{print $5}'), verified readable"

# Retention: keep the newest $KEEP. Old dumps are the only thing here that grows
# without bound, and a full disk is its own outage.
ls -1t "$BACKUP_DIR"/dida-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
  rm -f "$old" && echo "pruned old backup $(basename "$old")"
done

# ── 2. fetch ─────────────────────────────────────────────────────────────────
# A STALLED fetch used to hang the whole upgrade forever, silently. Measured on a
# 0.79 Mbit/s uplink: `git fetch` sat in anon_pipe_read for 100 minutes while its
# ssh child waited on a dead TCP connection — no timeout anywhere in that chain —
# and because the fetch is --quiet the log stayed 0 BYTES, which is
# indistinguishable from "still building". ssh drops a stalled connection in ~45 s,
# `timeout` is the belt, and every phase announces itself so an empty log now means
# "not started", never "stuck".
echo "== fetching =="
# EXTEND the repo's own ssh command, never replace it: a host may pin its deploy
# key with `core.sshCommand = ssh -i … -o IdentitiesOnly=yes`, and GIT_SSH_COMMAND
# takes precedence over that — so setting it blind fetches with no identity and the
# upgrade dies on "Could not read from remote repository".
GIT_SSH_BASE=$(git config --get core.sshCommand || echo ssh)
export GIT_SSH_COMMAND="$GIT_SSH_BASE -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
timeout 300 git fetch origin --quiet || { echo "FAILED: git fetch timed out or errored" >&2; exit 1; }

# Remember what is RUNNING before the tree moves. /version reads revision.json,
# which is derived from the checkout — so the checkout alone must never be taken as
# proof of what shipped, and this is what lets us put the claim back.
PREV_SHA=$(git rev-parse HEAD)

# ── 3. is this upgrade reversible by swapping images back? ───────────────────
# The rollback below restores the PREVIOUS images against the NEW schema. That is
# only safe while migrations are purely additive — old code ignores a new column.
# It is NOT safe across a migration that drops a column or a table or deletes rows:
# the old code then asks for something that no longer exists, and the rollback
# turns one broken deploy into two.
#
# So decide it here, from the actual diff, instead of asserting it in a comment.
# (This script used to carry the claim "DIDA's migrations are additive" — five of
# them are not: 0003, 0010, 0018, 0037, 0065.)
NEW_MIGRATIONS=$(git diff --name-only "$PREV_SHA" origin/main -- db/migrations/ ch/migrations/ || true)
DESTRUCTIVE=""
for m in $NEW_MIGRATIONS; do
  git show "origin/main:$m" 2>/dev/null \
    | grep -qiE '\bDROP[[:space:]]+(COLUMN|TABLE|TYPE)\b|\bDELETE[[:space:]]+FROM\b|\bTRUNCATE\b' \
    && DESTRUCTIVE="$DESTRUCTIVE $m"
done
if [ -n "$DESTRUCTIVE" ]; then
  echo "== NOTE: this upgrade contains a destructive migration =="
  for m in $DESTRUCTIVE; do echo "     $m"; done
  echo "   Image rollback is DISABLED for this run — old code cannot run on the new"
  echo "   schema. If health fails, restore $DUMP and check out $PREV_SHA."
fi

git reset --hard origin/main
# The UI's kit and the backend's home-core are public submodules fetched over
# https, so the deploy key has nothing to do with them; sync first in case a URL moved.
git submodule sync --quiet
timeout 120 git submodule update --init --quiet \
  || { git reset --hard "$PREV_SHA" --quiet; echo "FAILED: a submodule (kit, home-core) did not arrive" >&2; exit 1; }

# ── 3b. seeds the new tree spells differently ─────────────────────────────────
# .env lives only on the host and the compose file refuses to start without the
# seeds it names, so an upgrade that renames one has to carry the value across
# here — before anything below reads the compose file. Idempotent: a host that
# already has the new line is left alone.
env_set() { if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
env_drop() { sed -i "/^$1=/d" .env; }
if [ -z "$(env_read DIDA_LAN_IP)" ] && [ -n "$(env_read DIDA_MEDIA_BASE_URL)" ]; then
  lan=$(env_read DIDA_MEDIA_BASE_URL | sed -E 's|^https?://([^/:]+).*$|\1|')
  env_set DIDA_LAN_IP "$lan"
  echo "seed: DIDA_LAN_IP=$lan (from DIDA_MEDIA_BASE_URL)"
fi
if grep -qE '^COMPOSE_PROFILES=.*medialib' .env; then
  sed -i -E '/^COMPOSE_PROFILES=/{s/,?medialib,?/,/; s/=,/=/; s/,$//}' .env
  echo "seed: medialib dropped from COMPOSE_PROFILES (the music is OPUS's)"
fi
env_set DIDA_DOCKER_GID "$(stat -c %g /var/run/docker.sock)"
for gone in DIDA_MEDIA_BASE_URL DIDA_MEDIA_HTTP_PORT DIDA_MEDIA_RESCAN_SECONDS \
            DIDA_MUSIC_VOLUME_TYPE DIDA_MUSIC_VOLUME_OPTS DIDA_MUSIC_DEVICE \
            DIDA_SPOTIFY_CLIENT_ID DIDA_SPOTIFY_CLIENT_SECRET; do
  env_drop "$gone"
done

# ── 4. snapshot the running images ───────────────────────────────────────────
# By TAG, not by image id. On Docker 29 with the containerd snapshotter a
# container's `.Image` is a config digest that `docker tag` and `docker image
# inspect` cannot resolve, so an id-based snapshot could never be restored — and
# `docker compose images` aborts wholesale on the first unresolvable one, so the
# file came out EMPTY and the health gate was guarding nothing. Measured, not
# inferred: 42 of 48 services failed to restore in a dry run, all "No such image".
#
# Pointing :rollback at what :latest is RIGHT NOW also keeps that image alive, so
# the `docker image prune` at the end of the previous run cannot have taken it.
echo "== snapshotting the running images =="
ROLLBACK_TAGGED=0
for ref in $(docker compose config --images | sort -u); do
  case "$ref" in
    dida/*) ;;                      # ours; third-party images are pinned, never rebuilt
    *) continue ;;
  esac
  if docker image inspect "$ref" >/dev/null 2>&1; then
    docker tag "$ref" "${ref%:*}:rollback" && ROLLBACK_TAGGED=$((ROLLBACK_TAGGED+1))
  fi
done
if [ "$ROLLBACK_TAGGED" -eq 0 ]; then
  echo "FAILED: could not snapshot any image — refusing to upgrade without a way back" >&2
  exit 1
fi
echo "rollback snapshot: $ROLLBACK_TAGGED image(s) tagged :rollback"

# ── 5. build ─────────────────────────────────────────────────────────────────
# The base FIRST, unconditionally. dida_core is baked into dida/base and every
# service image is FROM it, so building a service without rebuilding the base
# gives new service code over yesterday's core — an ImportError at boot, or worse,
# silence. This ordering is the whole reason a `git pull && docker compose build
# api` by hand is the wrong way to upgrade.
# Retried, because a build over a thin uplink fails for a reason that goes away.
# The remote installation has 0.79 Mbit/s: a single dropped download aborts the
# whole `compose build`, and the operator is left re-running the upgrade by hand —
# after the tree has already moved. Docker's layer cache makes a retry cheap AND
# progressive: every layer that completed is reused, so each attempt starts further
# along than the last. Three tries, then give up loudly; a link that cannot carry
# one wheel in three attempts is not a blip.
#
# NOT a blanket retry on any failure: a build that fails because the code does not
# compile fails identically three times and takes three times as long to say so.
# That is the price of covering the case that actually happens here, and it is
# bounded — the compile error is in the output either way.
build_retry() {
  attempt=1
  while true; do
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -ge 3 ]; then
      echo "FAILED: $* did not complete after $attempt attempts" >&2
      return 1
    fi
    echo "build attempt $attempt failed — retrying (cached layers are kept)" >&2
    attempt=$((attempt + 1))
    sleep 5
  done
}

echo "== building base =="
build_retry docker compose --profile bases build base
echo "== building services =="
build_retry docker compose --profile "*" build

# ONLY NOW. `reset --hard` doesn't fire the hook that regenerates revision.json, so
# the upgrade has to do it — but doing it before the build made /version name a
# build that never happened: an api image once failed to fetch its apt packages,
# the run aborted, and the host reported the new revision while serving the old
# images. The tree has to move first (the build reads it); the CLAIM about what is
# running waits for something to actually be built.
./scripts/gen-revision.sh >/dev/null

# Non-root services run as uid 1000, so their /state dirs must be owned by it.
# Self-healing + idempotent: fixes a fresh install and any newly-added adapter's
# dir on every run, without a manual per-host chown. Only DIDA service dirs are
# touched; the infra data dirs (postgres/nats/clickhouse/mosquitto) keep their own
# image users. Root-running services can still write a 1000-owned dir, so this is
# safe across a rollback to older (root) images too.
if [ -n "$STATE_HOST" ]; then
  for d in "$STATE_HOST"/adapter-* "$STATE_HOST"/api "$STATE_HOST"/automation \
           "$STATE_HOST"/engine "$STATE_HOST"/journal "$STATE_HOST"/matter-bridge \
           "$STATE_HOST"/ingress "$STATE_HOST"/cloudflared; do
    [ -d "$d" ] && chown -R 1000:1000 "$d" 2>/dev/null || true
  done
fi

# ── 6. start and wait ────────────────────────────────────────────────────────
echo "== starting =="
# nats.conf is a bind mount, so a changed allow-list does not recreate the
# container: without the reload the old permissions stay live and every adapter
# publishing on a newly allowed subject is refused. Before `up`, so the adapters it
# recreates meet the new list on their first request, not half a minute later.
# Signalled from inside: `docker kill` of any signal marks the container manually
# stopped, and `unless-stopped` then leaves the bus down after the next reboot.
docker compose exec -T nats pkill -HUP -x nats-server 2>/dev/null || true
docker compose up -d --remove-orphans

health_bad() {
  # dida-keys is a one-shot: Exited (0) is it having done its job.
  docker compose ps -a --format '{{.Name}} {{.Status}}' \
    | grep -vE '^dida-keys Exited \(0\)' \
    | grep -E 'unhealthy|health: starting|Exited|Restarting|Created|Dead' || true
}

echo "waiting for health..."
# `ps` WITHOUT -a lists only running containers, so the 'Exited' arm below could
# never match: a service that died on boot simply vanished from the check and the
# run printed OK. It only ever caught anything because every service is `restart:
# unless-stopped` and a crash-looper shows as 'Restarting' — an unstated invariant
# the guard silently depended on.
bad=1
for _ in $(seq 1 18); do
  sleep 10
  bad=$(health_bad)
  [ -z "$bad" ] && break
done

if [ -n "$bad" ]; then
  echo "NOT HEALTHY:"
  echo "$bad"

  if [ -n "$DESTRUCTIVE" ]; then
    # Refusing IS the correct action. Swapping the images back here would put code
    # that predates the migration on top of a schema it cannot read — a second,
    # harder outage on top of the first, at the worst possible moment.
    echo "== NOT rolling back: this upgrade dropped schema the old images need ==" >&2
    for m in $DESTRUCTIVE; do echo "     $m" >&2; done
    echo "   Recover by hand:" >&2
    echo "     1) docker compose down" >&2
    echo "     2) git -C $ROOT reset --hard $PREV_SHA && ./scripts/gen-revision.sh" >&2
    echo "     3) restore the pre-upgrade dump: $DUMP" >&2
    echo "        (Settings -> Backup in the UI, or pg_restore -c -d $PG_DB)" >&2
    echo "     4) docker compose --profile bases build base && docker compose --profile '*' build && docker compose up -d" >&2
    exit 1
  fi

  echo "== rolling back to the previous images =="
  restored=0
  for ref in $(docker compose config --images | sort -u); do
    case "$ref" in dida/*) ;; *) continue ;; esac
    rb="${ref%:*}:rollback"
    if docker image inspect "$rb" >/dev/null 2>&1; then
      docker tag "$rb" "$ref" && restored=$((restored+1))
    fi
  done
  echo "restored $restored image(s) from :rollback"
  # The images are the previous build again, so the tree and the reported revision
  # must go back with them — otherwise /version names a commit whose code is not
  # running, which is the same lie a failed build used to tell.
  git reset --hard "$PREV_SHA" --quiet || true
  git submodule update --init --quiet || true
  ./scripts/gen-revision.sh >/dev/null || true
  docker compose up -d --remove-orphans
  docker compose exec -T nats pkill -HUP -x nats-server || true
  echo "verifying the restored stack..."
  rb_bad=1
  for _ in $(seq 1 12); do
    sleep 10
    rb_bad=$(health_bad)
    [ -z "$rb_bad" ] && break
  done
  if [ -z "$rb_bad" ]; then
    echo "ROLLBACK OK — previous stack restored ($(git rev-parse --short HEAD))"
    echo "the pre-upgrade backup is still at $DUMP"
  else
    echo "ROLLBACK STILL UNHEALTHY:"
    echo "$rb_bad"
    echo "the pre-upgrade backup is at $DUMP"
  fi
  exit 1
fi

docker image prune -f >/dev/null
docker builder prune -f --keep-storage 10GB >/dev/null
# -a here too: counting only what survived would report a happy number for a
# half-dead stack — the same blind spot the wait loop had.
echo "DEPLOY OK $(git rev-parse --short HEAD) — $(docker compose ps -aq | wc -l) containers up"

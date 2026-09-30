#!/usr/bin/env bash
# Deploy DIDA to one installation, or to all of them.
#
#   ./deploy/prod.sh home              # one instance
#   ./deploy/prod.sh all               # every instance in the inventory
#   ./deploy/prod.sh --list            # show what's registered
#   ./deploy/prod.sh --status          # show what each instance is RUNNING
#   ./deploy/prod.sh --dry-run all     # print the plan, change nothing
#   ./deploy/prod.sh root@10.0.0.5     # an installation not in the inventory
#
# The instance list lives in deploy/hosts.conf — adding a house is a line there,
# never another copy of this script. An argument that looks like an ssh target
# (contains @ or :) is deployed as a one-off with no flags, which is how a new
# installation gets its first deploy before it earns a line.
#
# The remote half is deploy/upgrade.sh — the SAME script an operator runs on
# their own box. It is shipped rather than kept on the host so this deploy always
# runs the version from the commit being deployed, and so the operator path and
# the maintainer path cannot drift apart: one of them being broken means both are.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INVENTORY="$SCRIPT_DIR/hosts.conf"
UPGRADE_SH="$SCRIPT_DIR/upgrade.sh"

DRY_RUN=0
DEPLOY_ALL=0
TARGETS=()

die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
say() { printf '\033[36m▶ %s\033[0m\n' "$*"; }
ok()  { printf '\033[32m✓ %s\033[0m\n' "$*"; }

[ -f "$UPGRADE_SH" ] || die "$UPGRADE_SH missing — there is no remote half to ship"

# --- inventory ------------------------------------------------------------

inventory_rows() { [[ -f "$INVENTORY" ]] && grep -vE '^\s*(#|$)' "$INVENTORY" || true; }

list_hosts() {
    printf '%-10s %-24s %-20s %s\n' NAME SSH PATH FLAGS
    inventory_rows | while read -r name ssh path flags; do
        printf '%-10s %-24s %-20s %s\n' "$name" "$ssh" "$path" "$flags"
    done
}

# What each installation is actually RUNNING, next to what this checkout would
# ship. The stamp is revision.json, written by upgrade.sh once something has
# actually been built — the checkout on the host moves before the build does, so
# `git log` over there can name a commit whose code is not running.
status_hosts() {
    local here rev
    here=$(git -C "$REPO_ROOT" rev-parse --short=8 HEAD 2>/dev/null || echo '?')
    printf '%-10s %-12s %s\n' NAME RUNNING STATE
    while read -r name ssh path flags; do
        # -n: without it ssh drains the loop's stdin and only the first host is
        # ever reported — which would make this command lie by omission.
        # `|| true`: under `set -e` a failed command substitution in an assignment
        # ends the script, so one unreachable box used to truncate the table after
        # it — the exact lie by omission the -n above is there to prevent.
        rev=$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$ssh" \
              "sed -n 's/.*\"sha\": *\"\([^\"]*\)\".*/\1/p' '$path/revision.json' 2>/dev/null" 2>/dev/null || true)
        [[ -n "$rev" ]] || rev='unreachable'
        printf '%-10s %-12s %s\n' "$name" "$rev" \
            "$(if [[ "$rev" == unreachable ]]; then echo 'could not be asked'
               elif [[ "$rev" == "$here" ]]; then echo 'up to date'
               else echo "differs — this checkout is $here"; fi)"
    done < <(inventory_rows)
}

row_for() {
    local want="$1" name ssh path flags
    while read -r name ssh path flags; do
        [[ "$name" == "$want" ]] && { printf '%s\t%s\t%s\n' "$ssh" "$path" "$flags"; return 0; }
    done < <(inventory_rows)
    # Not in the inventory: accept a raw ssh target so a brand-new installation
    # can be deployed before it has a line. No flags — a house that is not yet
    # registered is not the one carrying the family's phones or the public demo.
    case "$want" in
        *@*|*:*) printf '%s\t%s\t%s\n' "$want" "/mnt/docker/dida" ""; return 0;;
    esac
    return 1
}

# --- args -----------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --list) list_hosts; exit 0;;
        --status) status_hosts; exit 0;;
        --dry-run) DRY_RUN=1; shift;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        -*) die "unknown flag: $1";;
        all) DEPLOY_ALL=1
             while read -r n _; do TARGETS+=("$n"); done < <(inventory_rows); shift;;
        *) TARGETS+=("$1"); shift;;
    esac
done

# No argument still means "the home installation", which is what this script has
# always meant on its own. DIDA_PROD_HOST remains the fallback for a checkout
# with no inventory — an operator's own box, where hosts.conf never existed.
if (( ${#TARGETS[@]} == 0 )); then
    if inventory_rows | grep -q .; then
        TARGETS+=("$(inventory_rows | head -1 | awk '{print $1}')")
    elif [[ -n "${DIDA_PROD_HOST:-}" ]] || [[ -f "$REPO_ROOT/.env" ]]; then
        [[ -n "${DIDA_PROD_HOST:-}" ]] || \
            DIDA_PROD_HOST=$(grep -E '^DIDA_PROD_HOST=' "$REPO_ROOT/.env" | tail -1 | cut -d= -f2- | tr -d '"')
        [[ -n "${DIDA_PROD_HOST:-}" ]] || die "no inventory and no DIDA_PROD_HOST — nothing to deploy"
        TARGETS+=("$DIDA_PROD_HOST")
    else
        die "nothing to deploy — pass an instance name, or 'all' (see --list)"
    fi
fi

# --- one deploy at a time -------------------------------------------------
# Two deploys interleaving on one host race docker compose against itself and die
# on "container name already in use". Per INSTANCE, not per checkout: a single
# lock over the whole run would let a slow box block a fast one, and leaving the
# home installation waiting behind a remote house is exactly backwards.
lock_instance() {
    local name="$1" fd
    exec {fd}>"$REPO_ROOT/.deploy.lock.${name//[^A-Za-z0-9_.-]/_}"
    if ! flock -n "$fd"; then
        say "$name: another deploy is on it — waiting"
        flock "$fd"
    fi
    LOCK_FD="$fd"
}

# --- the deploying checkout must BE origin/main --------------------------
# There is no --allow-unpushed here, and that is not an oversight: upgrade.sh
# fetches origin/main ON THE HOST, so an unpushed commit cannot reach an
# installation at all. Without this check the deploy would quietly ship whatever
# someone else last pushed while you watched your own unpushed work scroll by.
require_pushed_head() {
    git -C "$REPO_ROOT" fetch --quiet origin main 2>/dev/null \
        || die "cannot reach origin — the hosts fetch from it, so there is nothing to deploy"
    local head origin dirty
    head="$(git -C "$REPO_ROOT" rev-parse HEAD)"
    origin="$(git -C "$REPO_ROOT" rev-parse origin/main)"
    [[ "$head" == "$origin" ]] \
        || die "HEAD ($(git -C "$REPO_ROOT" rev-parse --short HEAD)) is not origin/main ($(git -C "$REPO_ROOT" rev-parse --short origin/main)) — the hosts would fetch origin/main, not this. Push (or pull) first."
    dirty="$(git -C "$REPO_ROOT" status --porcelain | head -5)"
    if [[ -n "$dirty" ]]; then
        printf '\033[33m⚠ uncommitted changes are NOT deployed (the hosts fetch origin/main):\033[0m\n' >&2
        printf '%s\n' "$dirty" | sed 's/^/    /' >&2
    fi
    ok "deploying origin/main @ $(git -C "$REPO_ROOT" rev-parse --short HEAD)"
}
require_pushed_head

# --- the Android companion, built HERE ------------------------------------
# The signing keystore lives ONLY on this dev box (gitignored; no host signs) and
# a host would otherwise need the ~5 GB SDK toolchain. Rebuilt only when the
# committed android/ tree changed, so family phones are not prompted to update on
# an unrelated deploy. Built once per run even when several instances want it.
APK_READY=0
ensure_apk() {
    (( APK_READY )) && return 0
    local now built
    now="$(git -C "$REPO_ROOT" rev-parse --verify --quiet HEAD:android || echo none)"
    built="$(sed -n 's/.*"tree": "\([^"]*\)".*/\1/p' "$REPO_ROOT/android/dist/apk.json" 2>/dev/null || true)"
    if [[ ! -f "$REPO_ROOT/android/dist/dida.apk" || "$now" != "$built" ]]; then
        say "building companion APK (android/ tree changed: ${built:-none} -> $now)"
        "$REPO_ROOT/android/build.sh"
    fi
    APK_READY=1
}

ship_apk() {
    local ssh="$1" path="$2" m
    ensure_apk
    ssh -o BatchMode=yes -o ConnectTimeout=15 "$ssh" "mkdir -p '$path/android/dist'"
    scp -q "$REPO_ROOT/android/dist/dida.apk" "$REPO_ROOT/android/dist/apk.json" "$ssh:$path/android/dist/"
    # The car app ships when built (build.sh --auto); no tree-gate — its cadence
    # is manual until the Play internal track takes over distribution.
    for m in auto; do
        if [[ -f "$REPO_ROOT/android/dist/dida-$m.apk" ]]; then
            scp -q "$REPO_ROOT/android/dist/dida-$m.apk" "$REPO_ROOT/android/dist/$m.json" "$ssh:$path/android/dist/"
        fi
    done
}

# --- one instance ---------------------------------------------------------

deploy_one() {
    local name="$1" row ssh path flags LOCK_FD=
    row="$(row_for "$name")" || die "unknown instance '$name' (see --list)"
    IFS=$'\t' read -r ssh path flags <<<"$row"
    [[ "$flags" == "-" ]] && flags=""   # inventory placeholder for "none"

    say "$name — $ssh${flags:+ (flags=$flags)}"
    if (( DRY_RUN )); then
        echo "   would: ${flags:+ship the APK → }ship upgrade.sh → back up + verify the database → snapshot images → fetch origin/main → build → up → health-gate → roll back on failure"
        return 0
    fi
    lock_instance "$name"

    ssh -o BatchMode=yes -o ConnectTimeout=15 "$ssh" "test -d '$path'" \
        || { printf '\033[31m✗ %s: cannot reach %s, or %s does not exist there\033[0m\n' "$name" "$ssh" "$path" >&2; return 1; }

    case ",$flags," in *,apk,*) ship_apk "$ssh" "$path" ;; esac

    # Ship the remote half as a FILE and run it DETACHED, then watch it.
    #
    # Why: an installation can be reached through a Cloudflare tunnel that this
    # very deploy recreates. Streaming the script over `ssh … bash -s` means the
    # connection dies the moment the connector restarts — and the SIGHUP kills
    # docker compose MID-RECREATE, leaving the stack half-up with the tunnel down
    # and no way back in (that is exactly what happened to the Cabin instance on
    # 2026-08-07). setsid detaches it, so the deploy runs to completion — health
    # gate and rollback included — whatever happens to this ssh session.
    local log="/tmp/dida-deploy-${name//[^A-Za-z0-9_.-]/_}.log"
    local launcher="/tmp/dida-launch-${name//[^A-Za-z0-9_.-]/_}.sh"
    scp -q "$UPGRADE_SH" "$ssh:/tmp/dida-deploy.sh"

    # The marker line is how the watcher knows the run ended, and with what. It is
    # echoed by this wrapper rather than by upgrade.sh, so it is written even when
    # upgrade.sh dies on `set -e` half way through.
    if ! ssh -o BatchMode=yes -o ConnectTimeout=15 "$ssh" "rm -f '$log'; cat > '$launcher'" <<EOF
#!/bin/sh
DIDA_REPO='$path' bash /tmp/dida-deploy.sh
echo "DIDA_DEPLOY_DONE_\$?"
EOF
    then
        printf '\033[31m✗ %s: could not stage the remote half\033[0m\n' "$name" >&2
        return 1
    fi

    ssh -o BatchMode=yes -o ConnectTimeout=15 "$ssh" \
        "setsid sh -c \"sh '$launcher' > '$log' 2>&1\" >/dev/null 2>&1 &" \
        || { printf '\033[31m✗ %s: could not start the remote half\033[0m\n' "$name" >&2; return 1; }

    say "$name — running detached; watching $ssh:$log"
    # Up to ~90 min: a full rebuild over a 0.79 Mbit/s uplink genuinely takes that
    # long, and giving up here loses only the WATCHER — which would otherwise read
    # as a failed deploy that is in fact still running.
    local rc="" line last=""
    for _ in $(seq 1 540); do
        sleep 10
        line=$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$ssh" \
               "tail -n1 '$log' 2>/dev/null" 2>/dev/null || true)
        case "$line" in DIDA_DEPLOY_DONE_*) rc="${line#DIDA_DEPLOY_DONE_}"; break;; esac
        if [[ -n "$line" && "$line" != "$last" ]]; then
            printf '   %s\n' "$line"
            last="$line"
        fi
    done
    if [[ -z "$rc" ]]; then
        printf '\033[33m⚠ %s: stopped watching after 90 min — the remote half is detached and may still be running (%s: tail -f %s)\033[0m\n' \
            "$name" "$ssh" "$log" >&2
        return 1
    fi
    if [[ "$rc" != "0" ]]; then
        ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$ssh" "tail -25 '$log'" 2>/dev/null || true
        printf '\033[31m✗ %s: remote deploy failed (rc=%s)\033[0m\n' "$name" "$rc" >&2
        return 1
    fi

    # Refresh the public demo so it always shows the frontend that just shipped.
    # Opt-in: it needs .demo-pass (dev admin, for the recorder) and .cf-token, so
    # a deploy without them succeeds and just skips this.
    case ",$flags," in
      *,demo,*)
        if { [[ -n "${DIDA_DEMO_PASS:-}" ]] || [[ -s "$REPO_ROOT/.demo-pass" ]]; } && [[ -s "$REPO_ROOT/.cf-token" ]]; then
            say "refreshing public demo"
            "$SCRIPT_DIR/demo.sh" || printf '\033[33m⚠ demo refresh FAILED (the deploy above is fine)\033[0m\n' >&2
        else
            echo "   demo refresh skipped (needs .demo-pass + .cf-token)"
        fi
        ;;
    esac

    ok "$name deployed"
}

# `all` puts the first instance in the inventory in front of you and lets the
# rest carry on behind. The inventory is ordered by what it costs to have stale.
# Naming instances explicitly keeps every one of them in the foreground — if you
# asked for that box by name, you want to watch it.
foreground=("${TARGETS[@]}")
background=()
if (( DEPLOY_ALL && ${#TARGETS[@]} > 1 )); then
    foreground=("${TARGETS[0]}")
    background=("${TARGETS[@]:1}")
fi

failed=()
for t in "${foreground[@]}"; do
    deploy_one "$t" || failed+=("$t")
done

if (( ${#background[@]} && ! DRY_RUN )); then
    log="$REPO_ROOT/.deploy.background.log"
    setsid nohup "$0" "${background[@]}" >"$log" 2>&1 < /dev/null &
    say "${background[*]} continue in the background — $log"
elif (( ${#background[@]} )); then
    echo "   would: then deploy ${background[*]} in the background"
fi

if (( ${#failed[@]} )); then
    die "failed: ${failed[*]}"
fi
ok "done: ${foreground[*]}"

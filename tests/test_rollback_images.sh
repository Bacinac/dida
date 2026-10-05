#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
REAL_DOCKER=$(command -v docker)
REF="dida/audit-rollback-$$-$RANDOM:latest"
RB="${REF%:*}:rollback"
HELD="${REF%:*}:original"
CID=""
OLD_ID=""
NEW_ID=""
cleanup() {
  [ -z "$CID" ] || "$REAL_DOCKER" rm -f "$CID" >/dev/null
  "$REAL_DOCKER" image rm "$REF" "$RB" "$HELD" ${OLD_ID:+"$OLD_ID"} ${NEW_ID:+"$NEW_ID"} >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
for marker in old new; do
  printf 'FROM dida/base:latest\nENV SNAPSHOT_MARKER=%s\n' "$marker" \
    | "$REAL_DOCKER" build -q -t "$REF" - > "$WORK/$marker"
  if [ "$marker" = old ]; then
    OLD_ID=$("$REAL_DOCKER" image inspect "$REF" --format '{{.Id}}')
    CID=$("$REAL_DOCKER" run -d --network none "$REF" sleep 300)
    "$REAL_DOCKER" tag "$OLD_ID" "$HELD"
  else
    NEW_ID=$("$REAL_DOCKER" image inspect "$REF" --format '{{.Id}}')
  fi
done
export REAL_DOCKER CID OLD_ID NEW_ID
cat > "$WORK/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [ "$1 $2 $3" = "compose ps -aq" ]; then
  echo "$CID"
elif [ "$1 $2 $3" = "image ls -aq" ]; then
  printf '%s\n' "$OLD_ID" "$NEW_ID"
elif [ "$1" = inspect ]; then
  "$REAL_DOCKER" "$@" | awk -F'|' -v lost="${LOST_MANIFEST:-0}" \
    'BEGIN {OFS="|"} {$2="sha256:unresolvable-config"; if(lost) $4="sha256:missing-manifest"; print}'
else
  exec "$REAL_DOCKER" "$@"
fi
SH
chmod +x "$WORK/docker"
export PATH="$WORK:$PATH"
awk '/^snapshot_images\(\)/ {copy=1} /^echo "== snapshotting/ {copy=0} copy' \
  "$ROOT/deploy/upgrade.sh" > "$WORK/functions.sh"
source "$WORK/functions.sh"
test "$("$REAL_DOCKER" exec "$CID" printenv SNAPSHOT_MARKER)" = old
test "$("$REAL_DOCKER" image inspect "$REF" --format '{{.Id}}')" = "$NEW_ID"
snapshot_images "$WORK/map"
test "$("$REAL_DOCKER" image inspect "$RB" --format '{{.Id}}')" = "$OLD_ID"
restore_images "$WORK/map"
test "$("$REAL_DOCKER" image inspect "$REF" --format '{{.Id}}')" = "$OLD_ID"
"$REAL_DOCKER" tag "$NEW_ID" "$REF"
LOST_MANIFEST=1
export LOST_MANIFEST
if snapshot_images "$WORK/missing"; then
  echo 'FAILED: unresolved running image was accepted' >&2
  exit 1
fi
unset LOST_MANIFEST
test "$("$REAL_DOCKER" image inspect "$REF" --format '{{.Id}}')" = "$NEW_ID"
"$REAL_DOCKER" tag "$NEW_ID" "$RB"
if restore_images "$WORK/map"; then
  echo 'FAILED: changed rollback tag was accepted' >&2
  exit 1
fi
test "$("$REAL_DOCKER" image inspect "$REF" --format '{{.Id}}')" = "$NEW_ID"
test "$("$REAL_DOCKER" exec "$CID" printenv SNAPSHOT_MARKER)" = old
echo 'PASS: running manifest captured despite moved latest and unresolvable config digest'
echo 'PASS: rollback restores the original image and rejects missing or changed snapshots'

#!/usr/bin/env bash
# Build the signed DIDA Android APK inside the dida/android-builder container.
#
#   ./build.sh                  build → dist/dida.apk + dist/apk.json
#   ./build.sh --auto           build → dist/dida-auto.apk + .aab + auto.json
#   ./build.sh --init-keystore <public-host>  one-time: generate the release signing keystore
#
# Runs ONLY on the dev box (the keystore never leaves it); deploy/prod.sh ships
# dist/ to production as an artifact. Version = v<VERSION>.<git commit count>,
# the same scheme scripts/gen-revision.sh stamps for the rest of DIDA — but the
# APK is only rebuilt when android/ actually changed, so family phones are not
# prompted to update on every unrelated deploy.
set -euo pipefail
cd "$(dirname "$0")"

IMG="dida/android-builder:$(sha256sum builder.Dockerfile | cut -c1-12)"
RUN=(docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/project" -w /project
     -v dida-gradle-cache:/gradle-cache -e GRADLE_USER_HOME=/gradle-cache "$IMG")

# Context is the REPO ROOT, not android/: the builder image takes the shared
# docker/apt-https.sh rule from there like every other image does. .dockerignore
# keeps that context at ~8 MB, so it is no more expensive than android/ was.
docker image inspect "$IMG" >/dev/null 2>&1 || docker build -t "$IMG" -f builder.Dockerfile ..
# Docker creates a named volume root-owned; the build runs as us, so the cache is ours.
docker run --rm -v dida-gradle-cache:/gradle-cache "$IMG" \
  find /gradle-cache \( ! -user "$(id -u)" -o ! -group "$(id -g)" \) -exec chown -h "$(id -u):$(id -g)" {} +

if [[ "${1:-}" == "--init-keystore" ]]; then
  [[ -n "${2:-}" ]] || { echo "usage: ./build.sh --init-keystore <public-host>"; exit 1; }
  mkdir -p keystore
  [[ -f keystore/dida.keystore ]] && { echo "keystore already exists — refusing to overwrite (losing it bricks updates)"; exit 1; }
  STORE_PW=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
  "${RUN[@]}" keytool -genkeypair -v -keystore keystore/dida.keystore \
    -alias dida -keyalg RSA -keysize 4096 -validity 10950 \
    -storepass "$STORE_PW" -keypass "$STORE_PW" \
    -dname "CN=DIDA, O=boskovic.biz"
  cat > keystore/keystore.properties <<EOF
storeFile=keystore/dida.keystore
storePassword=$STORE_PW
keyAlias=dida
keyPassword=$STORE_PW
didaHost=$2
EOF
  echo "keystore generated. SHA-256 fingerprint for assetlinks.json:"
  "${RUN[@]}" keytool -list -v -keystore keystore/dida.keystore \
    -alias dida -storepass "$STORE_PW" | grep 'SHA256:'
  exit 0
fi

[[ -f keystore/keystore.properties ]] || { echo "no keystore — run ./build.sh --init-keystore first"; exit 1; }
# The app is built for one deployment: its public host drives the App Link filter
# and the default server address, and it pairs with this keystore through the
# host's assetlinks.json — so it lives beside the signing key.
DIDA_HOST="$(grep -E '^didaHost=' keystore/keystore.properties | cut -d= -f2- || true)"
[[ -n "$DIDA_HOST" ]] || { echo "no didaHost in keystore/keystore.properties — add didaHost=<public host phones reach>"; exit 1; }

# Version from git, same as gen-revision.sh. versionCode must be monotonic for
# Android to accept the update, so it is built from the whole version rather
# than the commit count alone: a count restarts whenever the history does.
BASE="$(tr -d ' \t\n\r' < ../VERSION)"
COUNT="$(git -C .. rev-list --count HEAD)"
[[ "$BASE" =~ ^([0-9]+)\.([0-9]+)$ ]] || { echo "VERSION must be MAJOR.MINOR, got '$BASE'"; exit 1; }
(( ${BASH_REMATCH[2]} < 100 && COUNT < 100000 )) || { echo "v${BASE}.${COUNT} does not fit the versionCode layout"; exit 1; }
CODE=$(( BASH_REMATCH[1] * 10000000 + BASH_REMATCH[2] * 100000 + COUNT ))
TREE="$(git -C .. rev-parse --verify --quiet "HEAD:android" 2>/dev/null || echo unknown)"

# The car app (:auto, IoT controls), distributed via the Play internal track
# (templated AA apps cannot sideload). Ships an AAB for Play next to the APK.
# Same keystore = the Play upload key. Music in the car is OPUS's app.
if [[ "${1:-}" == "--auto" ]]; then
  "${RUN[@]}" gradle --no-daemon -q \
    -PversionCode="$CODE" -PversionName="v${BASE}.${COUNT}" -PdidaHost="$DIDA_HOST" \
    :auto:assembleRelease :auto:bundleRelease
  mkdir -p dist
  for m in auto; do
    cp "$m/build/outputs/apk/release/$m-release.apk" "dist/dida-$m.apk"
    cp "$m/build/outputs/bundle/release/$m-release.aab" "dist/dida-$m.aab"
    SHA=$(sha256sum "dist/dida-$m.apk" | cut -d' ' -f1)
    SIZE=$(stat -c%s "dist/dida-$m.apk")
    cat > "dist/$m.json" <<EOF
{
  "versionCode": $CODE,
  "versionName": "v${BASE}.${COUNT}",
  "sha256": "$SHA",
  "size": $SIZE,
  "tree": "$TREE"
}
EOF
    echo "OK dist/dida-$m.apk + dist/dida-$m.aab v${BASE}.${COUNT} ($SIZE bytes)"
  done
  # --publish: push the fresh AABs to the Play internal testing track via the
  # play-publisher service account (publish.py). Phones then update from Play.
  if [[ "${2:-}" == "--publish" ]]; then
    docker run --rm -v "$PWD:/project" -w /project \
      -e UV_CACHE_DIR=/uvcache -v dida-uv-cache:/uvcache \
      --entrypoint sh dida/api:latest -c "python /project/publish.py"
  fi
  exit 0
fi

"${RUN[@]}" gradle --no-daemon -q \
  -PversionCode="$CODE" -PversionName="v${BASE}.${COUNT}" -PdidaHost="$DIDA_HOST" \
  :app:assembleRelease

mkdir -p dist
cp app/build/outputs/apk/release/app-release.apk dist/dida.apk
SHA=$(sha256sum dist/dida.apk | cut -d' ' -f1)
SIZE=$(stat -c%s dist/dida.apk)
cat > dist/apk.json <<EOF
{
  "versionCode": $CODE,
  "versionName": "v${BASE}.${COUNT}",
  "sha256": "$SHA",
  "size": $SIZE,
  "tree": "$TREE"
}
EOF
echo "OK dist/dida.apk v${BASE}.${COUNT} ($SIZE bytes, android tree $TREE)"

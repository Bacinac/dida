#!/usr/bin/env sh
# Regenerate uv.lock + docker/constraints.txt after editing any pyproject
# dependency (add / remove / change a version bound). Runs uv inside dida/base
# — containers-only, no host uv. The exported constraints.txt is what every
# `uv pip install` in the images pins against (see docker/base.Dockerfile).
#
#   docker/lock.sh              # re-lock to satisfy the current pyproject bounds
#   docker/lock.sh --upgrade    # bump everything to the latest allowed versions
#
# Then rebuild: docker compose build   (a rebuild now pins the exact locked set).
set -e
cd "$(dirname "$0")/.."   # repo root

UPGRADE=""
[ "$1" = "--upgrade" ] && UPGRADE="--upgrade"

docker run --rm -v "$PWD":/w -w /w -u "$(id -u):$(id -g)" \
  -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uvcache \
  --entrypoint sh dida/base:latest -c "
    uv lock $UPGRADE &&
    uv export --frozen --all-packages --no-hashes --no-emit-workspace \
      --no-annotate --no-header -o docker/constraints.txt
  "

echo 'Regenerated uv.lock + docker/constraints.txt. Rebuild with: docker compose build'

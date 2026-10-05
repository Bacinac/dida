#!/usr/bin/env sh
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
NAME="dida-audit-netmgr-$$"
cleanup() {
  docker rm -fv "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$NAME-socket" >/dev/null 2>&1 || true
  docker image rm "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker build -q -t "$NAME" - >/dev/null <<'DOCKERFILE'
FROM docker:29.7.2-dind
RUN apk add --no-cache iproute2
DOCKERFILE
docker run -d --rm --privileged --network none --memory=512m --memory-swap=512m \
  --name "$NAME" -v "$NAME-socket:/socket" --entrypoint dockerd "$NAME" \
  --host=unix:///socket/docker.sock --storage-driver=vfs --iptables=false --bridge=none >/dev/null
ready=0
for _ in $(seq 1 60); do
  if docker exec "$NAME" docker -H unix:///socket/docker.sock info >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
if [ "$ready" != 1 ]; then docker logs --tail 80 "$NAME"; exit 1; fi
docker exec "$NAME" ip link add eth0 type veth peer name eth1
docker exec "$NAME" ip link set eth0 up
docker exec "$NAME" ip link set eth1 up
docker save alpine:3 | docker exec -i "$NAME" docker -H unix:///socket/docker.sock load >/dev/null
docker run --rm --pull=never --network none --user 0 \
  -v "$NAME-socket:/socket" -v "$ROOT:/w:ro" -w /w \
  -e PYTHONPATH=/w/core/src:/w/services/netmgr/src --entrypoint python dida/netmgr:latest tests/netmgr_docker.py

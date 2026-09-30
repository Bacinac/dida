# Wrapper around Wez Furlong's govee2mqtt so the DIDA `govee` adapter can change
# credentials live. govee2mqtt only reads creds from env/CLI (both fixed at
# container CREATE), so a plain `docker restart` — which the adapter uses — can't
# refresh them. This entrypoint SOURCES /govee/govee.env (written by the adapter)
# at process start, so a restart re-reads it. An empty file = no GOVEE_EMAIL =
# clean LAN-only mode (no cloud, no crash). A static busybox provides the shell
# the upstream distroless image lacks.
# Pinned to digests for a reproducible build (same discipline as zigbee2mqtt/uv/python).
# Bump deliberately: `docker pull ghcr.io/wez/govee2mqtt:latest` (resp. busybox:musl),
# then paste the new `docker inspect --format '{{index .RepoDigests 0}}'` digest here.
FROM ghcr.io/wez/govee2mqtt:latest@sha256:d5427fa1524e3e6f87c59e5cbc59e782036a2b5274e4f96cfb581d844aa2b6dc
COPY --from=busybox:musl@sha256:8635836765b0c4c43970660219739baa58b0883c2e429e4b8918f7dd1519455c /bin/busybox /bin/busybox
# govee2mqtt runs as non-root (govee, uid 1000) and writes its API-mode cache
# (govee2mqtt-cache.sqlite) to /data. A NAMED volume (see compose) inits with the
# upstream image's govee-owned /data so the cache opens — a root-owned bind mount
# would crash it. Also: `. file` on a MISSING file makes a non-interactive shell
# exit (POSIX) which `|| true` can't catch, so guard with `if [ -f ]`; absent
# file = LAN-only mode.
ENTRYPOINT ["/bin/busybox", "sh", "-c", \
  "set -a; if [ -f /govee/govee.env ]; then . /govee/govee.env; fi; set +a; \
   exec /app/govee serve --govee-iot-key=/data/iot.key --govee-iot-cert=/data/iot.cert --amazon-root-ca=/app/AmazonRootCA1.pem"]

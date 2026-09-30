#!/bin/sh
# Point apt at an Ubuntu mirror that answers, over TLS, and make it fail loudly
# when none does. Every image runs this before its first apt-get.
#
# Any single source is the outage. On 2026-09-11 Canonical's archive stopped
# serving this site's address — :80 first, :443 within hours, while an off-site
# prober still got 200 — and the answer then was one regional mirror. On
# 2026-09-12 that mirror went dark from every host at once, while the archive
# answered again in 0.2-0.6 s, and a deploy died on it. So the script probes a
# list of independent operators and takes the first that serves the security
# pocket within seconds: measured, a dead entry costs its eight-second timeout
# and the next one carries the build.
#
# The probe, not apt, decides. apt's own `mirror+file:` list was tried first and
# did not move past a mirror that timed out; it retried the dead one and failed.
#
# Failing loudly is the other half. `apt-get update` exits 0 when every source
# failed — measured here, and on 2026-09-11 a degraded fetch lost the security
# index entirely and still returned success. A build carrying on without it is
# worse than a build that stops.
#
# Images that ship no CA bundle cannot run this — the probe and apt would both
# fail the handshake. ubuntu:24.04 is the one we hit, and the ingestor's intel
# stages borrow a bundle for exactly that reason.
set -eu

MIRRORS="https://mirrors.edge.kernel.org/ubuntu/ https://archive.ubuntu.com/ubuntu/ https://mirror.init7.net/ubuntu/ https://hr.archive.ubuntu.com/ubuntu/"

if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
    if [ ! -s /etc/ssl/certs/ca-certificates.crt ]; then
        echo "apt-sources: no CA bundle at /etc/ssl/certs/ca-certificates.crt — copy one in first" >&2
        exit 1
    fi
    . /etc/os-release
    pick=""
    for m in $MIRRORS; do
        if /usr/lib/apt/apt-helper -o Acquire::https::Timeout=8 -o Acquire::Retries=0 \
               download-file "${m}dists/${VERSION_CODENAME}-security/InRelease" /tmp/apt-probe \
               >/dev/null 2>&1; then
            pick=$m
            break
        fi
        echo "apt-sources: $m did not answer" >&2
    done
    rm -f /tmp/apt-probe
    if [ -z "$pick" ]; then
        echo "apt-sources: no Ubuntu mirror answered: $MIRRORS" >&2
        exit 1
    fi
    sed -i -E "s#^(URIs: *)https?://([a-z]+\.)?(archive|security)\.ubuntu\.com/ubuntu/?\$#\1${pick}#" \
        /etc/apt/sources.list.d/ubuntu.sources
fi

# deb.debian.org is a CDN and has stayed fast throughout; it only wants the scheme.
for f in /etc/apt/sources.list.d/debian.sources; do
    [ -f "$f" ] || continue
    sed -i 's#^\(URIs: *\)http://#\1https://#' "$f"
done

printf 'APT::Update::Error-Mode "any";\n' > /etc/apt/apt.conf.d/99-fail-loud

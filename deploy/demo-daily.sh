#!/usr/bin/env bash
# Daily demo refresh: pull today's house from production, anonymise it, publish.
#
# Without this the public demo shows the same sensor readings forever. Run from
# cron on the dev host:
#
#   30 4 * * *  /mnt/docker/dida/deploy/demo-daily.sh >> /mnt/docker/dida/state/demo-daily.log 2>&1
#
# Credentials come from files, not from the cron line: ./.demo-pass (dev admin,
# for the recorder) and ./.cf-token (Cloudflare Pages). Both are 0600 and
# gitignored.
#
# Two independent safety nets, both fail CLOSED: refresh-demo-data.py verifies
# the anonymised database, and deploy/demo.sh verifies the recorded fixtures.
# If production ever grows a name or place the rules don't know, nothing ships
# and yesterday's demo stays up — the correct failure.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== demo refresh $(date -Is) ==="
python3 scripts/refresh-demo-data.py
./deploy/demo.sh
echo "=== done $(date -Is) ==="

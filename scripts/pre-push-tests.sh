#!/usr/bin/env sh
# Local CI gate — invoked by scripts/git-hooks/pre-push. Runs tests/run.sh (lock
# check, ruff at the version uv.lock pins, the assertion suite) BEFORE a push leaves
# the machine, and blocks the push (non-zero exit) on any failure. Replaces the GitHub Actions workflow: same
# checks, on this (fast) box, no external dependency.
#
# FAST by design: it does NOT rebuild images — it reuses the locally-built images
# only for their DEPENDENCIES. Both ruff AND the tests run against the WORKING-TREE
# source (ruff lints the mount; the tests put the source on PYTHONPATH ahead of
# the baked packages), so a Python change is gated the moment you push — no rebuild
# needed. (A change to running CONTAINERS still needs a rebuild+restart to go live;
# a new dependency needs a rebuild so the image carries it.) Escape hatches:
#   DIDA_SKIP_TESTS=1 git push      # skip this gate once
#   git push --no-verify            # skip ALL pre-push hooks
set -eu
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

# Fresh clone / non-dev machine: nothing built to test against → skip, don't block.
if ! docker image inspect dida/base:latest >/dev/null 2>&1; then
  echo "pre-push: dida images not built locally — skipping test gate"
  exit 0
fi

echo "── pre-push: gate ──"
sh tests/run.sh

echo "✓ pre-push checks passed"

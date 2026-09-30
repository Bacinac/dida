#!/usr/bin/env sh
# One-time setup: activate the tracked git hooks and write the first revision.json.
#
# The hooks live in scripts/git-hooks (tracked + reproducible, unlike .git/hooks).
# core.hooksPath points git at them. Idempotent — safe to re-run after a clone.
set -eu
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

git config core.hooksPath scripts/git-hooks
chmod +x scripts/git-hooks/post-commit scripts/git-hooks/post-merge \
         scripts/git-hooks/pre-push scripts/gen-revision.sh scripts/pre-push-tests.sh

./scripts/gen-revision.sh
echo "git hooks active (core.hooksPath=scripts/git-hooks). revision.json:"
cat revision.json

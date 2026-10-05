#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mode=${1:---check}
gate=${2:-"$ROOT/tests/run.sh"}
case "$mode" in
  --check|--list) ;;
  *) echo "usage: $0 [--check|--list] [gate]" >&2; exit 2 ;;
esac
required=$(awk '/^REQUIRED="/ { reading=1; sub(/^REQUIRED="/, "") }
  reading { done=/"$/; gsub(/[\\"]/, ""); printf "%s ", $0; if (done) exit }' "$gate")
[ -n "$required" ] || { echo "FAILED: gate has no REQUIRED image catalog" >&2; exit 1; }
used=$({
  sed '/^[[:space:]]*#/d' "$gate" | grep -oE 'dida/[a-z0-9-]+:latest' | sed 's|dida/||; s/:latest$//'
  sed -nE 's/^adapter_test[[:space:]]+([a-z0-9-]+)([[:space:]].*)?$/adapter-\1/p' "$gate"
} | sort -u)
missing=""
for name in $used; do
  case " $required " in
    *" $name "*) ;;
    *) missing="$missing $name" ;;
  esac
done
[ -z "$missing" ] || { echo "FAILED: image catalog missing required entries:$missing" >&2; exit 1; }
for name in $required; do
  case "$name" in
    base) dockerfile="$ROOT/docker/base.Dockerfile" ;;
    adapter-*) dockerfile="$ROOT/adapters/${name#adapter-}/Dockerfile" ;;
    *) dockerfile="$ROOT/services/$name/Dockerfile" ;;
  esac
  [ -f "$dockerfile" ] || { echo "FAILED: no Dockerfile for dida/$name ($dockerfile)" >&2; exit 1; }
done
if [ "$mode" = --list ]; then
  printf '%s\n' $required
fi

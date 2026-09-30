#!/usr/bin/env bash
# Publish a DIDA release: the pushed HEAD becomes tag v<version> and a GitHub
# Release, which boskovic.biz reads and pins its install command to. DIDA builds
# its images on the installing machine, so nothing else ships; the release is
# refused until a stranger without an account can clone everything the tag names.
#
#   ./deploy/release.sh            publish
#   ./deploy/release.sh --dry-run  check everything, publish nothing
set -euo pipefail

cd "$(dirname "$0")/.."
REPO=Bacinac/dida

DRY_RUN=0
case "${1:-}" in
    "") ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac

die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ok()  { printf '\033[32m✓ %s\033[0m\n' "$*"; }

[[ -z "$(git status --porcelain)" ]] || die "uncommitted changes — a release is exactly what origin/main holds"
git fetch -q --tags origin main
SHA=$(git rev-parse HEAD)
[[ "$SHA" == "$(git rev-parse origin/main)" ]] || die "HEAD is not origin/main"
VERSION="$(tr -d ' \t\n\r' < VERSION).$(git rev-list --count HEAD)"
TAG="v$VERSION"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists"

# Asked anonymously, as the stranger running install.sh would, and
# over git: the anonymous REST API allows 60 requests an hour per address,
# which a dry run and a release from one network already spend.
PROBE=$(mktemp -d)
trap 'rm -rf "$PROBE"' EXIT
git init -q --bare "$PROBE"
public_commit() {
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
        git -C "$PROBE" fetch -q --depth=1 --filter=blob:none --no-tags "https://github.com/$1.git" "$2" 2>/dev/null
}
public_commit "$REPO" "$SHA" || die "$REPO@${SHA:0:8} is not publicly readable"
while read -r key url; do
    name=${key#submodule.}; name=${name%.url}
    path=$(git config -f .gitmodules "submodule.$name.path")
    pin=$(git ls-tree HEAD "$path" | awk '{print $3}')
    [[ "$url" =~ ^https://github\.com/([^/]+/[^/]+)$ ]] || die "submodule $path: $url is not a public https URL"
    public_commit "${BASH_REMATCH[1]%.git}" "$pin" || die "submodule $path@${pin:0:8} is not publicly readable"
done < <(git config -f .gitmodules --get-regexp '\.url$')
ok "$REPO@${SHA:0:8} and its submodules are public"

PREVIOUS=$(git describe --tags --abbrev=0 --match 'v[0-9]*' "$SHA^" 2>/dev/null || true)
if [[ -n "$PREVIOUS" ]]; then
    NOTES=$(git log --no-merges --format='- %s' "$PREVIOUS..$SHA")
else
    NOTES="First public release."
fi

if (( DRY_RUN )); then
    printf 'would tag %s as %s and release it on %s with these notes:\n%s\n' "${SHA:0:8}" "$TAG" "$REPO" "$NOTES"
    exit 0
fi
gh release create "$TAG" --repo "$REPO" --target "$SHA" --latest --title "DIDA $TAG" --notes "$NOTES" >/dev/null
git fetch -q origin "refs/tags/$TAG:refs/tags/$TAG"
ok "released $TAG — rebuild and deploy boskovic.biz to show it"

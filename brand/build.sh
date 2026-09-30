#!/bin/sh
# Regenerates every shipped DIDA mark (see build-assets.py) in a throwaway
# container: rsvg-convert renders the vectors, Pillow packs the .ico.
set -eu
cd "$(dirname "$0")/.."
docker run --rm -v "$PWD:/repo" -w /repo -e OWNER="$(id -u):$(id -g)" python:3.14-alpine sh -c '
	apk add --no-cache -q rsvg-convert >/dev/null
	pip install -q --root-user-action=ignore pillow
	python brand/build-assets.py
'

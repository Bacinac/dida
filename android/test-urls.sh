#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
img="dida/android-builder:$(sha256sum builder.Dockerfile | cut -c1-12)"
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/project:ro" -w /project "$img" sh -c '
  set -eu
  stdlib=$(ls /opt/gradle/lib/kotlin-stdlib-*.jar)
  java -cp "/opt/gradle/lib/*:/opt/gradle/lib/plugins/*" org.jetbrains.kotlin.cli.jvm.K2JVMCompiler \
    -no-stdlib -no-reflect -classpath "$stdlib" -d /tmp/url-tests \
    app/src/main/kotlin/biz/boskovic/dida/OwnTracksUrls.kt tests/OwnTracksUrlsTest.kt
  java -cp "/tmp/url-tests:$stdlib" biz.boskovic.dida.OwnTracksUrlsTestKt
'

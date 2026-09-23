#!/usr/bin/env bash
# Upload a built agent bundle to prod as a deployable artifact.
#
# The dashboard can LIST artifacts but cannot upload one (frontend/src/api.ts has no
# upload method), so until it grows a form this script is the only way to get something
# deployable into the fleet. Written for R1-test-1, kept because the gap is still there.
#
# The password is read with `read -s`: never echoed, never written to disk, and never
# passed as an argv, which would expose it in `ps` to every user on the box.
#
#     docs/runbooks/upload-artifact.sh                    # esp32s3, version from manifest
#     FF_TARGET=esp32 FF_VERSION=0.3.2 docs/runbooks/upload-artifact.sh
#
# Note the version is the artifact's LABEL, not something read out of the binary: the
# deploy UI shows it, and getting it wrong mislabels what the fleet is running.
set -euo pipefail

# Repo root, resolved from this script's own location, so it works from any cwd.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

BASE="${FF_BASE:-https://bingo.tvaroska.sk}"
TARGET="${FF_TARGET:-esp32s3}"
BIN="${FF_BIN:-$REPO_ROOT/agent/dist/$TARGET/app.bin}"
# Default to what the bundle says it is, rather than a hardcoded number that silently
# goes stale — the mislabelling this script is most likely to cause.
VERSION="${FF_VERSION:-$(python3 -c "
import json,sys
print(json.load(open('$REPO_ROOT/agent/dist/$TARGET/manifest.json'))['agent_version'])
" 2>/dev/null || echo "")}"

[ -n "$VERSION" ] || { echo "no FF_VERSION and no manifest to read it from" >&2; exit 1; }

[ -f "$BIN" ] || { echo "no such file: $BIN" >&2; exit 1; }

JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT
chmod 600 "$JAR"

read -rsp 'fleetforge admin password: ' FF_PW
echo

code="$(jq -Rn --arg p "$FF_PW" '{password:$p}' \
  | curl -sS -o /dev/null -w '%{http_code}' -c "$JAR" \
      -X POST "$BASE/v1/auth/login" \
      -H 'Content-Type: application/json' --data-binary @-)"
unset FF_PW

[ "$code" = "200" ] || { echo "login failed: HTTP $code" >&2; exit 1; }
echo "login ok"

echo "uploading $(wc -c <"$BIN") bytes as $TARGET / $VERSION ..."
curl -sS -b "$JAR" \
  -X POST "$BASE/v1/artifact?target=$TARGET&version=$VERSION" \
  -H 'Content-Type: application/octet-stream' \
  --data-binary "@$BIN" \
  -w '\nHTTP %{http_code}\n'

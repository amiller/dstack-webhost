#!/usr/bin/env bash
# Deploy the RFC 0034 notify receiver as a project on the daemon, then point
# DAEMON_NOTIFY_HOOK at its URL (env file on the daemon, operator step).
# Needs: BASE (daemon URL), TOKEN (create-scoped or owner), MATRIX_URL (full
# Matrix room-send endpoint incl. credentials).
set -euo pipefail
BASE=${BASE:?export BASE (daemon URL)}
TOKEN=${TOKEN:?export TOKEN (create-scoped or owner)}
: "${MATRIX_URL:?export MATRIX_URL (full Matrix room-send endpoint, credentials included)}"
NAME=${NAME:-notify-receiver}
HERE=$(cd "$(dirname "$0")" && pwd)

T=$(mktemp --suffix=.tgz); trap 'rm -f "$T"' EXIT
tar -czf "$T" -C "$HERE" --exclude='./.webhost-token' --exclude='./deploy.sh' .

RESP=$(curl -sS -X POST "$BASE/_api/projects" -H "Authorization: Bearer $TOKEN" \
  -F "manifest=$(jq -nc --arg n "$NAME" --arg u "$MATRIX_URL" '{name:$n, runtime:"deno", env:{MATRIX_URL:$u}}');type=application/json" \
  -F "files=@$T")
jq -c '{name, mode, approval}' <<<"$RESP"
echo "    $BASE/$NAME/  — set DAEMON_NOTIFY_HOOK=$BASE/$NAME/ in the daemon env"

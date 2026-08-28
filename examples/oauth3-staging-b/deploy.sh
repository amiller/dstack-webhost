#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
OAUTH3_SERVER_DIR=${OAUTH3_SERVER_DIR:-"$HOME/projects/oauth3-server"}
: "${TEE_DAEMON_URL:?source ~/.tee-daemon-staging.env first}"
: "${TEE_DAEMON_TOKEN:?source ~/.tee-daemon-staging.env first}"

test -f "$OAUTH3_SERVER_DIR/server/handler.ts"
test -f "$OAUTH3_SERVER_DIR/server/project.json"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
tar -C "$OAUTH3_SERVER_DIR/server" -czf "$tmp/oauth3b.tgz" .

curl -f -sS -X POST "$TEE_DAEMON_URL/_api/projects" \
  -H "Authorization: Bearer $TEE_DAEMON_TOKEN" \
  -F "manifest=@$ROOT/examples/oauth3-staging-b/project.json;type=application/json" \
  -F "files=@$tmp/oauth3b.tgz;type=application/gzip"

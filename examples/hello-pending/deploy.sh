#!/usr/bin/env bash
# RFC 0034 flow. First run creates the project with the provisioner token and stores the
# per-project token it gets back; every later run redeploys with only that token.
set -euo pipefail
BASE=${BASE:-https://pod.dstack.soc1024.com}
NAME=${NAME:-hello-pending}
PROV=~/.config/dstack-webhost/provisioner
HERE=$(cd "$(dirname "$0")" && pwd)
TOKFILE=$HERE/.webhost-token

T=$(mktemp --suffix=.tgz); trap 'rm -f "$T"' EXIT
tar -czf "$T" -C "$HERE" --exclude='./.webhost-token' .

if [ ! -f "$TOKFILE" ]; then
  echo "==> create $NAME (provisioner token, no owner token)"
  RESP=$(curl -sf -X POST "$BASE/_api/projects" -H "Authorization: Bearer $(cat "$PROV")" \
    -F "manifest={\"name\":\"$NAME\",\"runtime\":\"deno\"};type=application/json" -F "files=@$T")
  jq -r .token <<<"$RESP" > "$TOKFILE"; chmod 600 "$TOKFILE"
  echo "    approval: $(jq -c .approval <<<"$RESP")"
else
  echo "==> redeploy $NAME (per-project token)"
  curl -sf -X POST "$BASE/_api/projects/$NAME/redeploy" -H "Authorization: Bearer $(cat "$TOKFILE")" \
    -F "files=@$T" | jq -c '{name,mode,approval}'
fi
echo "    $BASE/$NAME/"

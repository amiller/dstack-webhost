#!/usr/bin/env bash
# Ship the env-redaction security fix to the hermes-staging daemon.
# Prereqs: `docker login ghcr.io` done; Phala dashboard open (restart can be finicky).
# Run:  bash ~/projects/tee-daemon/ship-fix.sh
set -euo pipefail

TEE="$HOME/projects/tee-daemon"          # canonical daemon source (has the fix)
HA="$HOME/projects/hermes-agent"         # where the staging deploy lives
IMG="ghcr.io/amiller/tee-socket-proxy"
TAG="env-redact-fix"
COMPOSE="$HA/docker-compose.staging.yaml"
ENVF="$HA/deploy-notes/.env.staging"     # full sealed env (TEE_DAEMON_TOKEN, MATRIX_*, ...)

echo "==> 1/4 build patched image from canonical source"
docker build -t "$IMG:$TAG" "$TEE"

echo "==> 2/4 push to ghcr"
docker push "$IMG:$TAG"
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' "$IMG:$TAG")
echo "    pushed: $DIGEST"

echo "==> 3/4 point staging compose at the new digest"
sed -i -E "s#${IMG}@sha256:[a-f0-9]+#${DIGEST}#" "$COMPOSE"
grep -n "$IMG" "$COMPOSE"

echo "==> 4/4 upgrade hermes-staging CVM (restarts daemon + hermes; sealed env re-supplied)"
phala deploy --cvm-id hermes-staging -c "$COMPOSE" -e "$ENVF" --wait

echo
echo "DONE. router-dashboard may need a redeploy:  bash ~/projects/webhost-apps/router-dashboard/deploy.sh"
echo "Verify the fix once a project is attested:"
echo "  curl https://915c8197b20b831c52cf97a9fb7e2e104cdc6ae8-8080.dstack-pha-prod7.phala.network/_api/projects/<name>  # env -> <redacted>"

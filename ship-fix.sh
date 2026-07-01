#!/usr/bin/env bash
# Ship the daemon image to a target CVM.  Usage: ship-fix.sh staging|prod
#   staging -> webhost-staging CVM (RFC 0023: the autonomous/Paseo deploy target)
#   prod    -> hermes-staging CVM (load-bearing; its env/token live only on the laptop)
# Prereqs: `docker login ghcr.io` done; Phala dashboard open (restart can be finicky).
set -euo pipefail

TARGET="${1:?usage: ship-fix.sh staging|prod}"
TEE="$HOME/projects/tee-daemon"          # canonical daemon source (has the fix)
HA="$HOME/projects/hermes-agent"         # where the deploy manifests live
IMG="ghcr.io/amiller/tee-socket-proxy"

case "$TARGET" in
  staging) CVM=webhost-staging; COMPOSE="$HA/docker-compose.webhost-staging.yaml"; ENVF="$HA/deploy-notes/.env.webhost-staging" ;;
  prod)    CVM=hermes-staging;  COMPOSE="$HA/docker-compose.hermes-prod.yaml";     ENVF="$HA/deploy-notes/.env.hermes-prod" ;;
  *) echo "unknown target '$TARGET' (valid: staging, prod)" >&2; exit 1 ;;
esac
TAG="$TARGET"

[ -f "$COMPOSE" ] || { echo "missing compose: $COMPOSE" >&2; exit 1; }
[ -f "$ENVF" ]    || { echo "missing env (not present in this environment?): $ENVF" >&2; exit 1; }

echo "==> 1/4 build patched image from canonical source"
docker build -t "$IMG:$TAG" "$TEE"

echo "==> 2/4 push to ghcr"
docker push "$IMG:$TAG"
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' "$IMG:$TAG")
echo "    pushed: $DIGEST"

echo "==> 3/4 point $TARGET compose at the new digest"
sed -i -E "s#${IMG}@sha256:[a-f0-9]+#${DIGEST}#" "$COMPOSE"
grep -n "$IMG" "$COMPOSE"

echo "==> 4/4 upgrade $CVM CVM (restarts daemon + hermes; sealed env re-supplied)"
phala deploy --cvm-id "$CVM" -c "$COMPOSE" -e "$ENVF" --wait

echo
echo "DONE ($TARGET -> $CVM)."

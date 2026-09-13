#!/usr/bin/env bash
# Ship the daemon image to a target CVM.  Usage: ship-fix.sh staging|prod|pod
#   staging -> webhost-staging CVM (RFC 0023: the autonomous/Paseo deploy target)
#   prod    -> hermes-staging CVM (load-bearing; its env/token live only on the laptop)
#   pod     -> oauth3-prod7, i.e. pod.dstack.soc1024.com (Base KMS: the new compose hash
#              is registered on-chain, so this target needs PRIVATE_KEY in the environment)
# Prereqs: `docker login ghcr.io` done; Phala dashboard open (restart can be finicky).
set -euo pipefail

TARGET="${1:?usage: ship-fix.sh staging|prod|pod}"
TEE="${TEE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"   # daemon source = this script's checkout (override with TEE=)
HA="$HOME/projects/hermes-agent"         # where the deploy manifests live
IMG="ghcr.io/amiller/tee-socket-proxy"
KMS=()
PRELAUNCH=()

case "$TARGET" in
  staging) CVM=webhost-staging; COMPOSE="$HA/docker-compose.webhost-staging.yaml"; ENVF="$HA/deploy-notes/.env.webhost-staging" ;;
  prod)    CVM=hermes-staging;  COMPOSE="$HA/docker-compose.hermes-prod.yaml";     ENVF="$HA/deploy-notes/.env.hermes-prod" ;;
  pod)     CVM=oauth3-prod7;    COMPOSE="$HA/docker-compose.prod7.yaml";           ENVF="$HA/deploy-notes/.env.prod9"
           : "${PRIVATE_KEY:?pod is Base-KMS: export PRIVATE_KEY (the Base signer) first}"
           KMS=(--kms base --private-key "$PRIVATE_KEY" --rpc-url "${ETH_RPC_URL:-https://mainnet.base.org}")
           # NOT optional. The prelaunch is what puts runsc/runsc-hostnet in Docker, and this
           # CVM's DAEMON_CONTAINER_RUNTIME names runsc-hostnet — deploy without it and the next
           # boot has no such runtime, so verify_configured_runtime() refuses to start the daemon
           # and the pod is dark. It also carries the wider default-address-pools.
           PRELAUNCH=(--pre-launch-script "$HA/deploy-notes/prelaunch-runsc-resilient.sh")
           [ -f "$HA/deploy-notes/prelaunch-runsc-resilient.sh" ] || { echo "missing prelaunch script — refusing to deploy a pod that would boot without runsc" >&2; exit 1; } ;;
  *) echo "unknown target '$TARGET' (valid: staging, prod, pod)" >&2; exit 1 ;;
esac
COMMIT=$(git -C "$TEE" rev-parse --short HEAD)
TAG="$TARGET-$COMMIT"

[ -f "$COMPOSE" ] || { echo "missing compose: $COMPOSE" >&2; exit 1; }
[ -f "$ENVF" ]    || { echo "missing env (not present in this environment?): $ENVF" >&2; exit 1; }

echo "==> 1/4 build patched image from canonical source ($COMMIT)"
docker build --build-arg GIT_COMMIT="$COMMIT" -t "$IMG:$TAG" "$TEE"

echo "==> 2/4 push to ghcr"
# Read the digest out of the push itself. `docker inspect .RepoDigests` was the old way and
# it is not reliable here: with a buildx/containerd image store the tag can be absent from the
# classic store even though the push succeeded, and the run dies at the point where the compose
# would have been pinned (2026-08-24, staging-64004d94).
PUSH_LOG=$(mktemp)
docker push "$IMG:$TAG" | tee "$PUSH_LOG"
SHA=$(sed -n 's/.*digest: \(sha256:[a-f0-9]\{64\}\).*/\1/p' "$PUSH_LOG" | tail -1)
rm -f "$PUSH_LOG"
[ -n "$SHA" ] || { echo "push printed no digest — refusing to guess what was pushed" >&2; exit 1; }
DIGEST="$IMG@$SHA"
echo "    pushed: $DIGEST"

echo "==> 3/4 point $TARGET compose at the new digest"
sed -i -E "s#${IMG}@sha256:[a-f0-9]+#${DIGEST}#" "$COMPOSE"
grep -n "$IMG" "$COMPOSE"

echo "==> 4/4 upgrade $CVM CVM (restarts every service in the compose; sealed env re-supplied)"
phala deploy --cvm-id "$CVM" -c "$COMPOSE" -e "$ENVF" "${KMS[@]}" "${PRELAUNCH[@]}" --wait

echo
echo "DONE ($TARGET -> $CVM)."

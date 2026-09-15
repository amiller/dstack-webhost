#!/usr/bin/env bash
# Ship the daemon image to a target CVM.  Usage: ship-fix.sh staging|prod|pod
#   staging -> webhost-staging CVM (RFC 0023: the autonomous/Paseo deploy target)
#   prod    -> hermes-staging CVM (load-bearing; its env/token live only on the laptop)
#   pod     -> oauth3-prod7, i.e. pod.dstack.soc1024.com (Base KMS: the new compose hash
#              is registered on-chain, so this target needs PRIVATE_KEY in the environment)
# `--no-build` (staging only) ships the image CI built for HEAD on the push to staging
# (.github/workflows/staging-image.yml) — no `docker login ghcr.io` needed on that path.
# Prereqs: `docker login ghcr.io` done (build path only); Phala dashboard open (restart can be finicky).
set -euo pipefail

TARGET="${1:?usage: ship-fix.sh staging|prod|pod [--no-build]}"
NOBUILD="${2:-}"
[ -z "$NOBUILD" ] || [ "$NOBUILD" = "--no-build" ] || { echo "unknown option '$NOBUILD' (only --no-build)" >&2; exit 1; }
[ -z "$NOBUILD" ] || [ "$TARGET" = staging ] || { echo "--no-build is staging-only: CI builds images only on pushes to staging" >&2; exit 1; }
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
# Tag contract with staging-image.yml: staging-<first 7 hex of the sha>. `--short` alone
# cannot be the contract — its width is object-count dependent (shallow checkout 7, full
# clone 8) and a width mismatch would make --no-build refuse an image CI did build.
# `--short` output is always a prefix of the sha, so its first 7 chars are machine-stable.
TAG="$TARGET-${COMMIT:0:7}"

[ -f "$COMPOSE" ] || { echo "missing compose: $COMPOSE" >&2; exit 1; }
[ -f "$ENVF" ]    || { echo "missing env (not present in this environment?): $ENVF" >&2; exit 1; }

if [ "$NOBUILD" = "--no-build" ]; then
  echo "==> 1/4 build: skipped (--no-build — CI built the image on the push to staging)"
  echo "==> 2/4 resolve digest of $IMG:$TAG from ghcr (package is public: anonymous pull, no ghcr login)"
  REPO_PATH="${IMG#ghcr.io/}"
  TOKEN=$(curl -fsS "https://ghcr.io/token?scope=repository:${REPO_PATH}:pull" \
    | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
  [ -n "$TOKEN" ] || { echo "ghcr refused the anonymous pull token for $REPO_PATH — cannot resolve $TAG" >&2; exit 1; }
  HDR=$(mktemp)
  CODE=$(curl -sS -o /dev/null -D "$HDR" -w '%{http_code}' \
      -H "Authorization: Bearer $TOKEN" \
      -H "Accept: application/vnd.oci.image.index.v1+json" \
      -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
      -H "Accept: application/vnd.oci.image.manifest.v1+json" \
      -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
      "https://ghcr.io/v2/${REPO_PATH}/manifests/$TAG") \
    || { rm -f "$HDR"; echo "ghcr request for $IMG:$TAG failed" >&2; exit 1; }
  [ "$CODE" = 200 ] \
    || { rm -f "$HDR"; echo "no CI-built image for this commit: $IMG:$TAG (ghcr HTTP $CODE) — push $COMMIT to staging so CI builds it, or ship without --no-build" >&2; exit 1; }
  SHA=$(sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest:[[:space:]]*\(sha256:[a-f0-9]\{64\}\).*/\1/p' "$HDR")
  rm -f "$HDR"
  [ -n "$SHA" ] || { echo "ghcr returned no Docker-Content-Digest for $TAG — refusing to guess" >&2; exit 1; }
  DIGEST="$IMG@$SHA"
  echo "    resolved: $DIGEST"
else
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
fi

echo "==> 3/4 point $TARGET compose at the new digest"
sed -i -E "s#${IMG}@sha256:[a-f0-9]+#${DIGEST}#" "$COMPOSE"
grep -n "$DIGEST" "$COMPOSE" || { echo "compose was not repinned to $DIGEST — no $IMG@sha256 pin to replace; refusing to deploy a stale image" >&2; exit 1; }

echo "==> 4/4 upgrade $CVM CVM (restarts every service in the compose; sealed env re-supplied)"
phala deploy --cvm-id "$CVM" -c "$COMPOSE" -e "$ENVF" "${KMS[@]}" "${PRELAUNCH[@]}" --wait

echo
echo "DONE ($TARGET -> $CVM)."

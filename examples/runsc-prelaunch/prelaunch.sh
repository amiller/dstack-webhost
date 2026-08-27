#!/bin/bash
# dstack prelaunch script: install gVisor's runsc and register it as three
# Docker runtimes — `runsc`, `runsc-hostuds` (--host-uds=open), and
# `runsc-hostnet` (--network=host). The hostnet
# variant keeps Sentry mediating every other syscall while letting app
# network syscalls reach the container's netns, so Docker's embedded DNS at
# 127.0.0.11 and container-name discovery work (dead under plain runsc's own
# netstack). Installs to /dstack/persistent/bin (writable ZFS); the dstack rootfs
# is read-only, and sha512sum is not present in the prelaunch environment, so
# verify with whichever sha512 tool is available.
#
# Verified working on dstack-dev-0.5.9 / DStack 0.5.9 (scarthgap), kernel
# 6.9.0-dstack, sysbox 0.6.7. After provisioning, `docker run --runtime=runsc
# alpine uname -a` reports gVisor's synthesised kernel (Linux 4.4.0 from
# Jan 2016) instead of the host's 6.9.0-dstack — the irrefutable signature
# of Sentry mediating syscalls.
set -euo pipefail

# NOTE 2026-06-25: 'latest' rolled AGAIN (recurrence of the 2026-06-12 break) — the
# pinned 8ecbf845… no longer matched gVisor's published runsc.sha512, the integrity
# check aborted boot, and hermes-staging wedged on EVERY reboot (incl. resize/redeploy)
# until diagnosed via `phala cvms serial-logs`. Permanent fix applied below: pinned the
# DATED/immutable release (release/20260622, the build 'latest' resolved to) instead of
# the floating 'latest' URL, so a future upstream roll can no longer wedge boot. To bump
# gVisor later, pick a newer release/<date>/ and set RUNSC_SHA512 to its published value.
RUNSC_URL="https://storage.googleapis.com/gvisor/releases/release/20260622/x86_64/runsc"
RUNSC_SHA512="6df95d09363dbd9ee5d5c889c1549b457e1783b039ff60a8f9f16f8c94c774a2ca2eef5b1c370e36b863f6b0407b53ba3c69051c6ef051253843dabf89a6de4e"
INSTALL_DIR="/dstack/persistent/bin"

verify_sha512() {
  local file=$1 expected=$2 actual=""
  if command -v sha512sum >/dev/null 2>&1; then
    actual=$(sha512sum "$file" | awk '{print $1}')
  elif command -v openssl >/dev/null 2>&1; then
    actual=$(openssl dgst -sha512 "$file" | awk '{print $NF}')
  elif command -v python3 >/dev/null 2>&1; then
    actual=$(python3 -c "import hashlib,sys;print(hashlib.sha512(open(sys.argv[1],'rb').read()).hexdigest())" "$file")
  else
    echo "[prelaunch] no sha512 tool found (sha512sum/openssl/python3)" >&2
    return 1
  fi
  if [ "$actual" != "$expected" ]; then
    echo "[prelaunch] sha512 mismatch: got $actual, want $expected" >&2
    return 1
  fi
}

echo "[prelaunch] mkdir $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

echo "[prelaunch] downloading runsc..."
curl -fsSL -o "$INSTALL_DIR/runsc" "$RUNSC_URL"
verify_sha512 "$INSTALL_DIR/runsc" "$RUNSC_SHA512"
chmod +x "$INSTALL_DIR/runsc"

echo "[prelaunch] registering runtime in /etc/docker/daemon.json"
mkdir -p /etc/docker
if [ -f /etc/docker/daemon.json ] && command -v jq >/dev/null 2>&1; then
  jq --arg p "$INSTALL_DIR/runsc" \
    '.runtimes.runsc = {"path": $p}
     | .runtimes["runsc-hostuds"] = {"path": $p, "runtimeArgs": ["--host-uds=open"]}
     | .runtimes["runsc-hostnet"] = {"path": $p, "runtimeArgs": ["--network=host"]}' \
    /etc/docker/daemon.json > /etc/docker/daemon.json.new \
    && mv /etc/docker/daemon.json.new /etc/docker/daemon.json
else
  cat > /etc/docker/daemon.json <<JSON
{
  "runtimes": {
    "runsc": {"path": "$INSTALL_DIR/runsc"},
    "runsc-hostuds": {"path": "$INSTALL_DIR/runsc",
                       "runtimeArgs": ["--host-uds=open"]},
    "runsc-hostnet": {"path": "$INSTALL_DIR/runsc",
                      "runtimeArgs": ["--network=host"]}
  }
}
JSON
fi

echo "[prelaunch] restarting docker..."
systemctl restart docker || service docker restart || true

echo "[prelaunch] runsc installed:"
"$INSTALL_DIR/runsc" --version
echo "[prelaunch] docker runtimes:"
docker info 2>/dev/null | grep -iE "runtime" || true
echo "[prelaunch] done"

#!/bin/bash
# dstack prelaunch script: install gVisor's runsc and register it as a Docker
# runtime. Installs to /dstack/persistent/bin (writable ZFS); the dstack rootfs
# is read-only, and sha512sum is not present in the prelaunch environment, so
# verify with whichever sha512 tool is available.
#
# Verified working on dstack-dev-0.5.9 / DStack 0.5.9 (scarthgap), kernel
# 6.9.0-dstack, sysbox 0.6.7. After provisioning, `docker run --runtime=runsc
# alpine uname -a` reports gVisor's synthesised kernel (Linux 4.4.0 from
# Jan 2016) instead of the host's 6.9.0-dstack — the irrefutable signature
# of Sentry mediating syscalls.
set -euo pipefail

RUNSC_URL="https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc"
RUNSC_SHA512="9844ad2493999c579d0b4bcbde99ad3ed98e9f0ee175d87003b2e4da2e61f22efe452c378503cb911edac1b627820cb7985326e52b44dafe5d17e593d4b9095a"
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
  jq --arg p "$INSTALL_DIR/runsc" '.runtimes.runsc = {"path": $p}' \
    /etc/docker/daemon.json > /etc/docker/daemon.json.new \
    && mv /etc/docker/daemon.json.new /etc/docker/daemon.json
else
  cat > /etc/docker/daemon.json <<JSON
{
  "runtimes": {
    "runsc": { "path": "$INSTALL_DIR/runsc" }
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

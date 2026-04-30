---
layout: default
---

# Isolation probe

A multi-tenant TEE makes a strong claim — *your code ran here, the others' code can't tamper with yours* — but the TEE attestation only covers what code is running. It does not by itself say what's keeping co-tenants from each other's secrets and files. That's a job for the OCI runtime that brings up tenant containers.

dstack-webhost runs every tenant under a runtime the substrate selects, and exposes the choice publicly at [`/_api/substrate`](https://github.com/amiller/dstack-webhost/blob/main/proxy/ingress.py). A relying party who wants to check the claim can deploy a tiny tenant whose only job is to expose its own kernel-namespace view, and then compare:

- **What the substrate says** — the daemon's stated runtime (e.g. `sysbox-runc`).
- **What the tenant sees** — the container's own `/proc/self/uid_map`, `user_ns`, etc.

A malicious substrate can lie in `/_api/substrate`, but it cannot forge the tenant's own `/proc`. That's the corroboration loop the probe demonstrates.

## Live demo

The probe is deployed as a [Layer-1 tenant](rfcs/0001-platform-vision.md) on hermes-staging:

→ **[hermes-staging.dstack-pha-prod7.phala.network/probe/](https://915c8197b20b831c52cf97a9fb7e2e104cdc6ae8-8080.dstack-pha-prod7.phala.network/probe/)**

The page fetches `/_api/substrate` and the tenant's own `/api/probe`, then renders a verdict. Source: [`apps/isolation-probe/`](https://github.com/amiller/dstack-webhost/tree/main/apps/isolation-probe).

## What the verdict means

`uid_map` is the signature. Under `sysbox-runc`, root inside the container (UID 0) is mapped to a non-root UID on the host — an active user-namespace remap. Plain `runc` shows the trivial mapping `0 0 4294967295`: root inside is root outside.

```
sysbox-runc:   "0     231072      65536"     ← shifted, ns boundary active
runc default:  "0          0 4294967295"      ← trivial, no remap
```

The probe's verdict is "consistent" when the substrate's claim and the tenant's `uid_map` agree on which regime is in force.

## What the substrate layers on top of the runtime

The OCI runtime is one layer. dstack-webhost adds three substrate-level mechanisms that go beyond what `sysbox-runc` alone provides:

- **Per-project Docker network.** Image-runtime tenants and `isolation: container` deno tenants each get `tee-proj-<name>-<mode>`; only the daemon is connected. A sibling tenant cannot reach another by IP, by container name, or by Docker DNS — the bridge drops the traffic. ([`proxy/runtimes.py::_ensure_project_network`](https://github.com/amiller/dstack-webhost/blob/main/proxy/runtimes.py))
- **Per-project named volume.** `isolation: container` tenants mount `tee-projdata-<name>` at `/data`. They never see other tenants' subdirs of the daemon's shared data volume. ([`proxy/runtimes.py::start_isolated`](https://github.com/amiller/dstack-webhost/blob/main/proxy/runtimes.py))
- **Scoped Deno permissions.** `isolation: container` deno tenants run under `--deny-env`, `--deny-ffi`, `--deny-run`, `--deny-sys`, `--allow-read` scoped to the project's own files. `manifest.env` is passed via Deno args (not env permission), so the handler sees `ctx.env` but cannot read other tenants' env values.

The shared deno runtime is *intentionally co-trust*: tenants there share a V8 isolate, share `/daemon-data` rw, and share a network. That's the model — opting into the shared runtime is opting into co-trust with its peers. Strong inter-tenant isolation requires `isolation: container` or `runtime: image`.

## Why sysbox-runc and not gVisor

[gVisor](https://gvisor.dev/) is the stronger answer: a userspace kernel intercepts syscalls, so the host kernel's attack surface visible to the tenant shrinks from ~330 syscalls to ~50. It addresses kernel-CVE-based escape as a class — sysbox does not.

The constraint inside Phala dstack: nested KVM is not exposed in the TDX CVM, so gVisor's KVM platform is unavailable; the ptrace/systrap platform is the only viable mode (slower, but functional). `runsc` is not present on stock dstack today, but provisioning it does not require a coordinated base-image change. Two attestation-safe paths exist within the existing dstack flow:

- **Prelaunch script.** dstack accepts a `--pre-launch-script` argument; the script's hash is part of the launch payload, hence measured. The script downloads `runsc` pinned by sha512 (verified before install), writes it to `/dstack/persistent/bin`, registers it in `/etc/docker/daemon.json`, and restarts docker. The pinned hash ties the actual bytes to the measured script. **This works:** [`apps/runsc-prelaunch/`](https://github.com/amiller/dstack-webhost/tree/main/apps/runsc-prelaunch) is the script we tested. A throwaway dstack CVM provisioned with it ran `docker run --runtime=runsc alpine uname -a` and reported gVisor's synthesised kernel `Linux 4.4.0 #1 SMP Sun Jan 10 15:06:54 PST 2016` instead of the host's `6.9.0-dstack` — Sentry is mediating syscalls.
- **Privileged bootstrap container.** A short-lived container in the compose with `privileged: true` and the host filesystem mounted, whose image bakes the `runsc` binary in directly. The image digest is in the compose, so it's measured by dstack; the binary that lands on the host is fixed by the digest. Same shape as the existing `ssh-debug` sidecar. *Not wired up; the prelaunch path was sufficient for the demo.*

What's still on the work list: measure ptrace/systrap performance against the workloads we run, decide whether `DAEMON_CONTAINER_RUNTIME=runsc` should be the operator switch on hermes-staging (sysbox-runc is fine for the present substrate; runsc is the next-tier upgrade for kernel-CVE protection).

`sysbox-runc`, meanwhile, is already registered as a Docker runtime on stock dstack and needs nothing installed. It's a hardened `runc` (automatic user-namespace remap, virtualised `/proc` and `/sys`, scoped capabilities) — meaningfully better than plain runc against namespace/capability escapes, weaker than gVisor against kernel CVEs. It's the isolation layer that's *running today* on this CVM; gVisor is the next, more ambitious step.

## Open attack surface

What this stack does *not* address, and is worth arguing about:

- **Kernel CVEs.** `sysbox-runc` hardens the namespace/capability boundary; it does not shrink the host kernel's syscall attack surface. A tenant that exploits a kernel bug (`io_uring`, BPF, etc.) escapes the CVM and breaks every tenant's quote. The CVM is currently on `6.9.0-dstack` from May 2024, which has a year of post-release CVEs. This is the structural reason for the gVisor argument above. The provisioning path is now demonstrated to work via [prelaunch script](https://github.com/amiller/dstack-webhost/tree/main/apps/runsc-prelaunch); flipping hermes-staging onto runsc is a separate operational decision (perf, hermes compat) rather than a missing capability.
- **Weakest-link tenant on a shared kernel.** Every tenant on the CVM shares one Linux kernel, so the effective isolation floor for *any* tenant is whatever the *weakest* tenant's runtime exposes. A gVisor-protected app on the same CVM as a runc-protected sibling is no better off than the sibling against kernel-CVE escape — once the host kernel is compromised by the weak link, an attacker reads Sentry-protected memory from outside Sentry's mediation. This is the structural reason `DAEMON_CONTAINER_RUNTIME` is a CVM-level switch rather than a per-manifest field; per-project runtime choice would be a foot-gun for the attestation claim. (On hermes-staging the `hermes` and `ssh-debug` peer services run under default runc because they're outside the substrate's "design under test" boundary; for a serious deployment they'd need to share the floor.)
- **Per-tenant resource limits.** No cgroup memory/CPU/disk caps today. A hostile tenant can OOM the host or starve sibling CPU. Easy to add — this is unfinished work, not a hard problem.
- **Same-host side channels.** `/proc/loadavg`, RAM pressure, page-cache timing, CPU contention — `sysbox-runc` does not virtualise these. A tenant pair could plausibly establish a covert channel by modulating load and timing read-back. Quantifying the bandwidth would be its own demo, not yet built.
- **Daemon TCB.** The daemon (`tee-daemon`) owns Docker, owns each tenant's per-project network, and is the authority behind `/_api/substrate`. A daemon compromise is a total compromise. The TEE attestation pins the daemon's image hash; a relying party walks the trust chain to the public source and audits it as part of the substrate.
- **Inter-mode bridges.** The daemon connects itself to every per-project network so it can reverse-proxy. A tenant that compromised the daemon's network stack from inside its own network could in principle pivot. The exposed surface is the daemon's bridge interface plus its ingress port — small, but not zero.
- **Verifier coverage of image-runtime tenants.** The current verifier page checks `tree_hash` against GitHub; for image-runtime tenants the integrity claim is `image_digest` against a registry, and the verifier page does not yet branch on this. Audit machinery, not a runtime hole.

A natural next demo is a sibling probe that actually tries each of these channels and reports what worked. Not built yet — the substrate-level fixes (network, volume, Deno perms) closed the easier channels first; the side-channel work is harder and lower priority.

## Trying it on your own CVM

The image is already published — you can deploy it directly without rebuilding:

```bash
# Make sure the substrate is configured for sysbox-runc:
#   environment:
#     - DAEMON_CONTAINER_RUNTIME=sysbox-runc

# Deploy as a tenant:
curl -X POST https://<cvm>/_api/projects \
  -H "Authorization: Bearer $TEE_DAEMON_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"probe","runtime":"image",
       "image":"ghcr.io/amiller/tee-isolation-probe:v1",
       "image_port":8000}'

# Visit
open https://<cvm>/probe/
```

To rebuild from source (`apps/isolation-probe/`), see the [README there](https://github.com/amiller/dstack-webhost/tree/main/apps/isolation-probe).

## Related

- [Substrate endpoint](https://github.com/amiller/dstack-webhost/blob/main/proxy/ingress.py) — what the daemon exposes about its runtime configuration
- [Platform vision (RFC 0001)](rfcs/0001-platform-vision.md) — Layer 1 vs Layer 2, attestation vs isolation
- [Verifier skill](verify-skill.md) — agent-runnable audit of a deployed project

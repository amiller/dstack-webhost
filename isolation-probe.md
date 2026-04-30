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

## Picking the runtime

The choice on this substrate is between three OCI runtimes, all of which can be made available on stock dstack:

- **`runc`** — the default. No hardening; same syscall surface as a non-containerised process.
- **`sysbox-runc`** — Nestybox's hardened runc. Adds automatic user-namespace remap, virtualised `/proc` and `/sys`, scoped capabilities. Already registered as a Docker runtime on stock dstack — no install step. Meaningful against namespace/capability escapes; **does not shrink the host kernel's syscall attack surface**, so kernel CVEs (`io_uring`, BPF, etc.) remain reachable.
- **`runsc` (gVisor)** — Google's userspace kernel. Sentry intercepts syscalls; only ~50 of ~330 host syscalls are reachable from a tenant, behind a strict seccomp filter. Addresses kernel-CVE escape as a class. Not present on stock dstack, but installable via prelaunch script ([`apps/runsc-prelaunch/`](https://github.com/amiller/dstack-webhost/tree/main/apps/runsc-prelaunch)) — the script's hash is in the measured launch payload, the binary is pinned by sha512, the trust chain stays intact. (A privileged bootstrap container in the compose is a parallel attestation-safe path; not wired up because the prelaunch path was sufficient.)

For nested KVM (which would let gVisor use its faster KVM platform): not exposed in dstack TDX. The ptrace/systrap platform is what we use, with the perf cost measured below.

## Recommendation

**`runsc` (gVisor) is the recommended runtime for this substrate.** Attestation is the product; the relying-party threat model genuinely values kernel-CVE resistance, which is the only thing `runsc` adds and `sysbox-runc` does not. The perf cost is ~20% throughput and ~2× p99 tail on a 1-vCPU CVM (see below) — tolerable for the small handlers and Layer-1 image apps this substrate hosts. **`sysbox-runc` is the right fallback** for tenants whose workloads can't accept that perf hit (a database under load, a latency-critical endpoint). **Plain `runc` has no place** on a multi-tenant TEE substrate when sysbox-runc is right there at zero cost.

This was a working assumption the substrate started under (`DAEMON_CONTAINER_RUNTIME=sysbox-runc` is what hermes-staging runs today); the comparison below is the data that turned it from "deferred until we measure" into a real recommendation.

## Measured perf cost

A controlled comparison on the same dstack CVM (1 vCPU, 2 GB RAM), flipping `DAEMON_CONTAINER_RUNTIME` between the three runtimes and redeploying the probe each time. Workload: 20 concurrent clients hitting `/probe/api/probe` for 45s — moderately syscall-heavy (4 `/proc` reads + JSON serialise + HTTP serve per request).

| Runtime | rps | p50 | p90 | p95 | p99 |
|---|---|---|---|---|---|
| `runc` | 221.6 | 80.3ms | 92.4ms | 111.2ms | 179.6ms |
| `sysbox-runc` | 221.8 | 80.2ms | 93.2ms | 111.8ms | 182.7ms |
| `runsc` (gVisor) | 175.2 | 101.6ms | 118.5ms | 137.4ms | **395.8ms** |

**`sysbox-runc` is free.** Indistinguishable from `runc` on throughput and every latency percentile. Sysbox does its hardening at container *startup* (user-namespace setup, proc/sys virtualisation), not per syscall — once the container is running, syscalls go straight to the host kernel like with runc. Zero per-request cost.

**`runsc` costs about 21% throughput, 27% median latency, and 2.2× p99 tail latency.** Sentry intercepts every syscall in userspace; on a 1-vCPU CVM with a syscall-heavy workload, that's the visible price. Workloads that are mostly network-bound without much per-request kernel work will see less. Databases, file-heavy services, anything chatty with the kernel will see more.

The runtime-choice implication:

- **`sysbox-runc`** is the right *default floor* — namespace/cap hardening for free.
- **`runsc`** is an *opt-in upgrade* when the threat model justifies ~20% throughput and ~2× p99 tail for kernel-CVE resistance. It's not a free lunch.
- Plain `runc` is no faster than sysbox-runc; there's no performance reason to prefer it on a multi-tenant TEE substrate.

## A covert channel runsc actually closes: `/proc` is host-wide under runc and sysbox-runc

While digging for residual cross-tenant channels on hermes-staging, the most striking real finding: `/proc/loadavg`, `/proc/stat`, `/proc/meminfo`, and `/proc/uptime` are all *host-wide* under `runc` and `sysbox-runc`. Reading them from inside any tenant gives back the host's actual values — including activity contributed by sibling tenants.

Side-by-side, on the same host:

```
                  HOST        runsc        sysbox-runc          runc
/proc/loadavg     1.89...     0.00 0.00    1.89... (host)       1.89... (host)
/proc/uptime      1001 s      0.28 s       0.05 s               1003 s (host)
/proc/stat cpu    173390      0 0 0 0      173496 (host)        173533 (host)
/proc/meminfo     2444 free   3824 (sandboxed)  2389 (host)     2379 (host)
```

This is a working covert channel under runc/sysbox-runc:

1. Tenant A busy-loops a CPU core in a deliberate pattern (e.g. spin 1s, idle 1s).
2. Tenant B reads `/proc/loadavg` once per second.
3. B reconstructs A's signal from the load oscillation.

Bandwidth is low (loadavg updates over seconds), but the channel is structurally there and easy to demonstrate. Sysbox doesn't help — its FUSE-virtualised `/proc` covers some files but lets `loadavg`/`stat`/`uptime`/`meminfo` pass through to the host's view.

**`runsc` (gVisor) closes this channel** by synthesising those `/proc` entries inside Sentry — the tenant only ever sees its own kernel-state-equivalent, never the host's. Sentry IS the kernel for the workload, so there's no host view to leak.

This is the strongest concrete reason to prefer runsc on this substrate that surfaced during the comparison work — the kernel-CVE argument was structural but abstract; this is a demonstrable cross-tenant information leak that flipping the runtime literally closes. (Higher-bandwidth channels — cache timing, memory pressure modulation — are harder to characterise and likely remain to some degree under any runtime when tenants share physical hardware.)

## Known limitation: gVisor + Docker embedded DNS

When the substrate runs under `runsc`, tenants that need outbound DNS resolution can hit a real interaction bug between gVisor's sandbox network stack and Docker's embedded DNS resolver at `127.0.0.11`. Docker forces `nameserver 127.0.0.11` into `/etc/resolv.conf` for any container on a user-defined bridge network (which is most of them, including the per-project `tee-proj-<name>-<mode>` networks the substrate creates). gVisor's own netstack doesn't route to that address. Result: DNS lookups return "Connection refused" even though the host's actual resolver is reachable.

`HostConfig.Dns` (the compose `dns:` field) does not fix this — it only changes the upstream resolvers Docker's embedded DNS forwards to, not the embedded DNS address Docker writes into `/etc/resolv.conf`. Workarounds for affected tenants:

- Bake static IPs or known hosts into the application image instead of relying on DNS.
- Use the application's own DNS-over-HTTPS resolver (bypasses the OS resolver entirely).
- Run the tenant as `runtime: runc` (loses gVisor's protection but DNS works).
- Pull the tenant out of the user-defined bridge entirely — daemon-side substrate change, not yet implemented.

This affected hermes during the live runsc migration on hermes-staging; hermes is currently kept on `runc` for that reason. The isolation-probe doesn't make outbound network calls so it's not affected.

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

# RFC 0025: Attested container capabilities

**Status**: Draft

## Summary

Let a project request elevated Linux capabilities (`CAP_NET_ADMIN`) and device
access (`/dev/net/tun`) on its container — but **only** when the project is
`mode:"attested"`, so the elevated grant is always on the verifiable attested
surface. The motivating use case: run an OpenVPN-TCP→SOCKS sidecar *inside* a
single image project (Phala blocks UDP, so WireGuard/userspace is out; OpenVPN
needs a TUN device + `NET_ADMIN`, which the daemon otherwise strips).

The rule that makes this safe: **caps ⟹ attested**. Elevation forces
transparency — there is no reachable state where a project holds `NET_ADMIN`
that a verifier cannot see.

## Problem

`create_container` (`proxy/docker_client.py`) intentionally exposes no
`CapAdd`/`Devices`/`Privileged`, and the app docker-proxy strips
`NetworkMode`. So a kernel-TUN VPN cannot run as an app. The only options were
a standalone (non-daemon) CVM or a blanket capability grant — the latter being
*invisible privilege*: an app silently gets `NET_ADMIN` and you must trust the
operator. We want the privilege to be a **legible, verifiable claim** instead,
in the spirit of the capability-statement work (consumer-checkable evidence).

## Design

1. **Manifest fields.** `cap_add: [str]`, `devices: [str]` on the `Project`
   model. They serialize via `asdict()` into the stored manifest and the
   RFC-0015 public verification read, so a verifier reading
   `/_api/projects/<name>` sees exactly what was granted.

2. **The gate: caps ⟹ attested.** `deploy()` / `_deploy_image()` reject
   `cap_add`/`devices` unless `mode == "attested"`. `start_isolated()` /
   `start_image()` re-check at apply time (belt-and-suspenders): caps are passed
   to Docker only for attested projects. A dev-mode project — which is hidden
   from the verification endpoint — can never hold caps.

3. **Binding the grant to the attestation.**
   - *Source projects (git/deno):* caps are read preferentially from the
     **repo-committed `project.json`**, which is inside the `files/` tree, so
     `tree_hash`/`git_tree_sha` commits to them. A verifier who fetches the
     source commit confirms the declared caps.
   - *Image projects:* there is no source tree, so the grant is bound by the
     **append-only audit `detail`** (recorded at deploy, inside the TEE) plus the
     pinned **`image@sha256` digest** (immutable code). The audit entry now
     includes `cap_add`/`devices` and the image digest.

4. **Defense in depth.** The app-facing docker-proxy also strips
   `CapAdd`/`Devices`/`Privileged`, so the *only* path to caps is the daemon's
   own gated create.

## Blast radius

`CAP_NET_ADMIN` is a per-network-namespace capability. Each project container
runs in its own netns on its own bridge (`tee-proj-<name>-<mode>`). A
NET_ADMIN app:

- **Can** manage *its own* netns — bring up `tun0`, set routes/iptables (the VPN
  use case). Confined to its namespace.
- **Cannot** reconfigure the host netns or other projects' namespaces; it does
  not grant host root.
- Shares an L2 segment only with the daemon's own proxy endpoint *to this same
  project* (the daemon attaches itself to the per-project bridge to proxy).
- **Residual:** caps + `/dev/net/tun` enlarge the shared-host-kernel attack
  surface (netlink/netfilter/tun). Namespace confinement holds for behavior, not
  for a kernel CVE. This is the standard "VPN sidecar with NET_ADMIN" profile,
  acceptable for an attested, source-visible app inside a TEE. Note `runsc`
  (gVisor) may not support `/dev/net/tun`; a TUN app runs under `runc`/`sysbox`.

## Manifest example

```json
{
  "name": "brave-spi",
  "runtime": "image",
  "image": "ghcr.io/amiller/brave-vpn-spi@sha256:...",
  "image_port": 3000,
  "mode": "attested",
  "cap_add": ["NET_ADMIN"],
  "devices": ["/dev/net/tun"],
  "env_passthrough": ["OPENVPN_USER", "OPENVPN_PASS", "OVPN_CONFIG_BASE64"]
}
```

## Out of scope

Multi-container projects and a built-in VPN/egress adapter (RFC 0002's future
work). This RFC is the minimal change that lets a single image project carry its
own VPN, with the grant made verifiable rather than ambient.

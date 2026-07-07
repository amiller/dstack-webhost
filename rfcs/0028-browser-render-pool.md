# RFC 0028: Browser Runtime & Render Pool

**Status**: Draft

## Summary
Make the logged-in browser a **first-class daemon runtime** instead of one shared external CVM.
The daemon runs a **pool of isolated browser containers** in the pod; a browser-path read (twitter
timeline, any screenshot/DOM plugin) **leases** one, the requester's cookie jar is injected for that
lease via the credential broker, the browser is driven, the result returns, and the container is
**reset (cookies/storage cleared) before the next lease**. This replaces `login-with-anything` — a
single Neko CVM with one global session that any pod user could read and any one call could lock.

## Problem (what today's single CVM gets wrong)
Today `oauth3/browser.ts` drives one external CVM (`login-with-everything`) over its bridge:
- **Single shared session.** `browserFeed` reads *whatever is currently logged in* and does not inject
  per-user cookies — so a non-owner who connects timeline-peek sees the **owner's** timeline. It's
  single-tenant wearing a multi-tenant costume.
- **No isolation / no reset.** Concurrent `/session` calls clobber one global `currentSession`; a jar
  from one user lingers for the next.
- **No fairness.** One slow `/eval` or `/navigate` (30–90 s) occupies the only browser — any user can
  starve the pool of one.
- **Was wide open.** The bridge had no auth and was public (`/eval` = arbitrary JS in the logged-in
  browser). Closed as an **interim** by a shared-secret lock (bridge Bearer + lwa-net exemption +
  stripped `/health` + removed root-SSH sidecar), but that's a stopgap on a single-tenant box.

## Design
1. **A `browser` runtime in the daemon** (`proxy/runtimes.py`), alongside `deno`/`image`. The daemon
   already spawns per-project isolated containers with health checks (RFC 0009), restart (0010), and
   an egress proxy; a browser container (Neko/Chromium + the bridge) is one more managed image.
2. **A warm pool.** N browser containers on the pod's internal network behind the egress proxy. A
   **lease** picks a free one (or spawns up to a cap); a **queue** with per-lease timeouts provides
   fairness so one caller can't starve the rest.
3. **Per-lease jar injection, per-lease reset.** On lease, the requester's jar is injected for the
   target domain **only**; on release, the container is reset (clear cookies/storage/nav) before it
   returns to the pool. No cross-user residue, no shared login. `browserFeed`'s "read the current
   session" antipattern is deleted — every read carries its own jar.
4. **Auth by broker, not a shared secret.** The lease is a **scoped delegation** (RFC 0018): the jar
   reaches the leased browser through the broker socket, path-scoped to that plugin/subject, not a
   flat bridge password. The interim `BRIDGE_SECRET` retires with the external CVM.
5. **Attested like any project.** Browser containers carry the daemon's `mode` axis (#16), so a
   consumer can verify the render code (RFC 0020) the same way as any app.

## What finishes it (the dependency ladder — answering "if we wait for dev to lock in")
| Piece | State | Needed for the pool |
|---|---|---|
| Daemon container runtime + health/restart/durability | **built** (0009/0010/0017) | the substrate a browser runtime reuses |
| **Credential broker** — scoped, expiring jar delegations | **spec'd** (RFC 0018), impl pending | how a leased browser gets *only* this user's jar |
| Attestation evidence | **spec'd** (0020) | so the render is verifiable |
| **`browser` runtime type** (spawn Neko+bridge as a managed runtime) | **new** | the core of this RFC |
| **Lease/pool/queue + per-lease reset** | **new** | isolation + fairness (the multi-tenant fix) |
| oauth3 `browser.ts` → pod pool endpoint (drop the external CVM) | **new** (small) | cutover |

So: the substrate exists; the **new work is the browser runtime + the lease/pool/reset layer**, riding
the **credential broker (0018)** for per-user jar scoping. Until 0018 lands, a leased browser can't be
given a properly-scoped jar, so 0018 is the gating dependency.

## Non-goals / relations
- Not a replacement for **reification** (RFC 0001): the goal is still to turn browser tasks into cheap
  replayable API calls; the pool serves the **irreducible** browser cases, not every read.
- Supersedes the interim bridge-secret lock and the single `login-with-anything` CVM.
- Debug access to pool containers follows RFC 0026 (promotion-gated).

## Decisions
- **Isolation model — DECIDED: a fresh container per lease.** A lease gets a dedicated browser
  container for its full duration; on release the container is destroyed, not reset-and-reused. This
  buys the strongest isolation (no reset-completeness risk — nothing to clear because nothing is
  reused) at the cost of cold-spawn latency, mitigated by the warm pool. This makes the "no
  cross-session bleed" guarantee structural, not a scrubbing best-effort.
- **Metering — DECIDED: account render-time per lease.** Because a lease is a discrete
  container-for-a-duration, on release it writes `{plugin, subject, active_render_ms}` (and peak vCPU
  if cheap to sample) to the audit log. This is simultaneously the fairness signal and the unit of a
  future per-subject quota/price — browser-time as a scoped, metered capability. First cut ships the
  accounting hook only; pricing/quota is later.

## Open questions
- **Warm-pool sizing vs. cold-spawn latency** — Neko/Chromium cold start is seconds; a small warm pool
  hides it but costs memory. Autoscale on queue depth?
- **Egress binding** — each lease's egress should be locked to the target domain (the jar's cookie
  domains), consistent with RFC 0003's egress-lock default.

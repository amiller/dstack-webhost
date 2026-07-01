# RFC 0027: Per-App Attestation Granularity

> Renumbered from 0025 during the security merge sprint: 0025 was independently
> taken by "Attested container capabilities" (#56) on main while this was drafted.

## Summary
Today the TDX quote attests the **webhost daemon**, and the daemon vouches "project
P = tree_hash H" by deriving a KMS-rooted per-path key (`GetKey /tee-daemon/projects/<name>`,
`proxy/ingress.py:_api_attest`). The hardware never measures or references the app; the H↔P binding
is the daemon's say-so, recorded in the promotion manifest (`proxy/deploy.py:promote`). A consumer
that needs a *hardware* quote of the app itself can't get one (RFC 0020 §"Note on (3)"; RFC 0019
deployment-granularity).

This RFC designs the path to per-app attestation: a spectrum from a hardware-rooted *binding quote*
on the shared daemon (buildable now, on the runc staging CVM) to full per-app CVMs (one dstack
instance per app, hardware-measured app compose). It defines what gets measured, who signs what, and
how a consumer's `verify()` distinguishes a **daemon-vouched** binding from an **app-attested** one —
surfaced as a new `attestation_kind` in RFC 0020's evidence bundle.

## Problem
The four-layer chain in RFC 0020 has a soft joint at layer 3 (per-app binding):
```
TDX quote (hardware)  →  attests the daemon's MRTD/RTMR (its compose_hash, OS image)
KMS signature_chain   →  roots the daemon's derived per-path key on base-prod
per-path key          →  binds the *string* "/tee-daemon/projects/<name>", not H
manifest / promotion  →  asserts name → tree_hash H   ← daemon's word, unmeasured
app process           →  runs in a shared Deno isolate or sibling container
```
Hardware attests the **daemon**. The step from "approved daemon code" to "this app is H" is an
in-daemon assertion. Three classes of consumer need more: a counterparty releasing funds to *this
app*; an auditor wanting a non-repudiable H↔endpoint binding rooted below daemon logic; a high-value
tenant wanting blast-radius isolation (today all shared-runtime tenants share one V8 isolate and one
derived-key namespace). Three unused dstack primitives sit behind the allow-list
(`proxy/dstack_proxy.py`): `GetQuote` (fresh TDX quote with caller-chosen 64-byte `report_data`),
`EmitEvent` (extend RTMR3 with a measured, logged event), and per-app-CVM provisioning.

## Design Space
The axis is **where the app's identity (H) enters the hardware measurement.**

### (a) Per-app CVM — strongest, heaviest
Each app its own dstack instance: own `app_id`, own compose measured into MRTD/RTMR, own `GetQuote`.
The app's code *is* the certified TCB; `verify()` gets a real per-app quote, no daemon in layer 3.
Cost: a CVM per app (TDX memory/boot, a base-prod `DstackApp` entry + on-chain approval per app,
per-app gateway routing/provisioning/recovery). Requires base-prod + per-app CVM provisioning we
don't have on staging. Defeats the "shared daemon hosts many cheap apps" shape for the long tail.

### (b) Per-app sub-attestation on the shared daemon — lighter
Daemon stays one CVM. Per attested app it produces a **hardware-rooted binding quote**: `GetQuote`
with `report_data = SHA-512("tee-daemon/app-attest/v1" ‖ app_id ‖ name ‖ tree_hash ‖ app_pubkey)`,
where `app_pubkey` is the app's KMS-derived per-path key (already sanitized in `_sanitize_getkey`).
The quote is signed by the **TDX quoting key (hardware)**; MRTD/RTMR still measure the *daemon*, but
`report_data` now carries the exact H and the app's signing pubkey, non-repudiably, below daemon
logic. Optionally also `EmitEvent("tee-daemon/promote", {name,tree_hash,commit,image_digest})` →
every later daemon quote carries the app-promotion log in measured RTMR3 history.

**Trust delta vs (a):** the measured TCB is still the daemon, not the app. You gain: the
H↔name↔app_pubkey binding is hardware-signed/RTMR3-measured (a compromised daemon can't backdate or
disclaim it without producing a quote; the app_pubkey lets the app sign live statements chaining to
that quote). You do NOT gain: app code in the measured TCB, or isolation stronger than the shared
runtime. Cost: one extra `GetQuote` + `EmitEvent` per promote. Works on the existing runc staging CVM.

### (c) Hybrid — promote hot/high-value apps to their own CVM
Default every app to (b); a per-app flag (or consumer request) *promotes* an app to (a): provision a
dedicated CVM, re-issue the binding under a real per-app quote, flip `attestation_kind` to `app-cvm`.
Matches RFC 0019's "start shared, promote the flagship" posture + the dev→attested ladder.

### Trade matrix
| | (a) per-app CVM | (b) binding quote on shared daemon | (c) hybrid |
|---|---|---|---|
| Hardware measures app code | yes (compose in MRTD) | no (daemon TCB; H in report_data/RTMR3) | per-app |
| H↔endpoint binding | hardware-measured | hardware-signed assertion by daemon TCB | per-app |
| Isolation | full CVM | shared isolate/container (pre-existing gap) | per-app |
| On-chain entries | one DstackApp per app | one (the daemon) | 1 + N_hot |
| Marginal cost / app | a CVM | one GetQuote + EmitEvent | mixed |
| Buildable on staging CVM now | no (needs provisioning + base-prod) | yes | partial |
| Long-tail-app friendly | no | yes | yes |

## Recommended Architecture
**Adopt (b) now as the universal baseline, structured so (c) is a later promotion — do not block on
(a).** (b) closes the actual soft joint (an *unmeasured* JSON assertion → a *hardware-signed* one) at
near-zero cost on the CVM we already run, and is honest about its trust level via `attestation_kind`.
(a) is right for a specific high-value app, not the platform default, and needs per-app CVM
provisioning + base-prod we lack on staging. (c) is just (a) applied selectively once (b) makes the
two kinds distinguishable to consumers.

### Concrete mechanism (b)
1. **At promote** (`proxy/deploy.py:promote`), after the manifest is finalized: derive `app_pubkey`
   via `GetKey /tee-daemon/projects/<name>` → `_sanitize_getkey`; compute `report_data =
   SHA-512(DOMAIN ‖ app_id ‖ name ‖ tree_hash ‖ app_pubkey)`, `DOMAIN = b"tee-daemon/app-attest/v1"`
   (to the 64-byte field); `GetQuote(report_data)` → `binding_quote` (raw TDX bytes + collateral
   pointer); persist it with `tree_hash`/`commit_sha`/`image_digest`; `EmitEvent("tee-daemon/promote",
   {...})` so the promotion is in RTMR3's measured-event log.
2. **Keep `GetQuote` daemon-side.** Tenant apps reach dstack only through the filtered broker
   (`/run/broker/dstack.sock`, scoped to `/tee-daemon/`); the binding-quote call is daemon-side, so no
   tenant can mint quotes for paths it doesn't own. If app-side `GetQuote` is ever exposed it must be
   report_data-domain-scoped the same way `GetKey` is path-scoped.
3. **Serve it.** Extend `_api_attest`/`_api_verification` to return `binding_quote`, `report_data`,
   and the preimage (`app_id`,`name`,`tree_hash`,`app_pubkey`) so a verifier recomputes `report_data`
   and checks equality. The per-path `signature_chain` stays — the KMS root for `app_pubkey`.
4. **What `verify()` distinguishes.**
   - `attestation_kind == "daemon-vouched"`: verify the TDX `binding_quote` (DCAP/QVL + collateral),
     confirm MRTD/RTMR == the **approved daemon** compose on base-prod, recompute `report_data` and
     confirm equality, confirm `app_pubkey`'s `signature_chain` roots in the KMS. Fact: `tree_hash` is
     hardware-bound *by the daemon TCB*; app runtime integrity depends on the named/approved daemon.
   - `attestation_kind == "app-cvm"`: `binding_quote`'s MRTD/RTMR == the **app's own** compose; no
     daemon in layer 3; `tree_hash`/image hardware-measured directly.
   Same bundle shape + same `verify()` path; only the measured-identity target + the `attestation_kind`
   fact differ. `verify()` renders **no verdict** — policy lives in the consumer (RFC 0020).

## Composition with RFC 0020's Evidence Bundle
Add `attestation_kind` and replace the bare binding pointer with a `binding` block:
```jsonc
"attestation_kind": "daemon-vouched" | "app-cvm",
"app": { "...": "...",
  "binding": {
    "kind": "report-data-quote" | "app-quote",
    "binding_quote": "...", "report_data": "0x...",
    "preimage": { "domain","app_id","name","tree_hash","app_pubkey" },
    "app_pubkey": "...",
    "promote_event": { "rtmr": 3, "event": "tee-daemon/promote", "digest": "..." } } }
```
For `daemon-vouched`, `platform_quote` and `binding_quote` are the *same daemon quote* (report_data is
the only app-specific content); `verify()` checks measured identity == approved daemon. For `app-cvm`,
`platform_quote == binding_quote` is the app's own CVM quote; measured identity == the app. `Facts`
gains `attestation_kind` + `binding_verified`; no new network calls (preimage recomputation is local).

## Phasing
**Phase 1 — buildable on the current runc staging CVM:** the `promote()` changes (derive pubkey,
report_data, `GetQuote`, `EmitEvent`, persist); serve the `app.binding` block; `verify()`'s
`daemon-vouched` path. Tests: promote → bundle → `verify()` returns `daemon-vouched`/`binding_verified:
true`/correct `tree_hash`; tamper `tree_hash` → `report_data` mismatch in `errors[]` (the quote is
unforgeable, so the lie is caught); confirm the promote event in RTMR3 via a fresh daemon quote.
(On staging the quote verifies even if `onchain_approved` is `chain_id 0` — an RFC 0020 fact, not a
blocker; `attestation_kind` keeps it truthful.)

**Phase 2 — needs per-app CVM provisioning + base-prod (RFC 0019):** a `promote-to-cvm` action
(provision a dedicated instance, register its `DstackApp` on base-prod, route its `app_id`, re-issue
the bundle as `app-cvm`); `verify()`'s `app-cvm` measured-identity target; durable per-app state
(RFC 0017). This is the hybrid (c); the platform default stays Phase-1 (b).

## Open Questions & Hard Trade-offs
- **Is (b) honestly "per-app attestation"?** It's *hardware-rooted per-app binding*, not *per-app
  measurement*. The truth line is exactly `attestation_kind`; don't let "attested app" collapse the
  two. Do we even let consumers request (a), or is "read the open daemon code + (b)" the product
  position for everything but a few flagships?
- **report_data is 64 bytes, single-shot per quote.** One quote binds one app (chosen: a quote per
  promote), or pack a Merkle root of all attested apps into one daemon quote + per-app inclusion
  proofs (cheaper steady-state, more `verify()` complexity). RTMR3 `EmitEvent` is the natural
  "all apps in one rooted structure" — decide whether RTMR3 events are canonical and report_data
  redundant, or vice-versa.
- **Quote freshness / revocation.** A binding quote is point-in-time; stale bundles still verify after
  teardown/re-promote. Need a liveness signal — the app signs a fresh nonce with `app_pubkey`, or the
  bundle carries the current RTMR3 log so a re-promote shows up.
- **Shared-isolate reality.** (b) hardware-binds H but the shared Deno runtime still runs many tenants
  in one `--allow-all` V8 isolate. `daemon-vouched` must never be read as "isolated"; only (a) closes
  that. Document it.
- **base-prod gating.** (a) requires base-prod (pha-prod returns chain_id 0). (b)'s quote verifies
  anywhere but its *on-chain approval* fact does not — decide whether `daemon-vouched` may ship from a
  non-anchored ecosystem at all, or only as a dev fact.

## Out of Scope
The appraisal/verdict layer (RFC 0020 / 0022); durable state for promoted per-app CVMs (RFC 0017);
secret delegation to attested apps (RFC 0018); closing the shared-isolate multi-tenancy gap (its own
isolation RFC — this RFC makes the trust *level* honest, it does not raise it).

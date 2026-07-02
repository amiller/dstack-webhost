# RFC 0029: Attested Apps with Declared Operator Debug

**Status**: Draft

## Summary
Add a trust category **between** fully-locked attested and dev/debug: an app whose code is
attested (measured, source-bound) **and** whose manifest *declares* that the operator retains a
scoped debug capability (attach/exec/logs/read). The declaration is part of the measured surface,
so opening a debug session does not secretly void the attestation — the possibility was already in
the attestation. A consumer's `verify()` facts read `attested; operator_debug: enabled`, and the
consumer decides for itself. The dishonest state is *hidden* debug, not *declared* debug.

## Problem
RFC 0026 treats operator debug on an attested app as a contradiction: attestation is
"all-or-nothing on the running code," so you **un-promote to debug** (voids the attestation) and
re-promote afterward. That is correct for the *binary* it assumes — "attested" tacitly means "no
human can touch this." But that binary is what pushes operators to either (a) not attest apps they
know they'll need to poke, or (b) keep a privileged side channel (the old `ssh-debug` sidecar, host
SSH) that silently makes a "promoted" app shell-reachable — the `hermes-staging` contradiction RFC
0026 itself names. Both are *less* honest than saying so. The real-world need for scoped operator
access to a running attested app doesn't go away because the model forbids naming it.

RFC 0025 already solved the shape of this problem for a different privilege: an elevated grant
(`CAP_NET_ADMIN`, `/dev/net/tun`) is allowed **only** on the attested surface, so "there is no
reachable state where a project holds the privilege that a verifier cannot see." Operator debug is
another such elevated capability. This RFC applies the same rule — *elevation forces transparency* —
to debug access, and reconciles RFC 0026's refusal into a disclosure.

## Design
Debug is a **declared capability on an attested app**, not a third mode. It reuses three pieces
already specified: RFC 0025's caps⟹attested gate, RFC 0026's audited-broker session mechanics, and
RFC 0020's evidence bundle.

1. **Manifest field.** `operator_debug: { enabled: bool, principals: [str], scope: [...] }` on the
   `Project` model (mirrors `cap_add`/`devices` in RFC 0025). `scope` names what a session may do
   (`exec`, `logs`, `read:dataDir`) and `principals` who may open one. It serializes via `asdict()`
   into the stored manifest and the RFC-0015 public read.

2. **The gate: `operator_debug ⟹ attested`.** `deploy()`/`promote()` reject the field unless
   `mode == "attested"`; there is no *undeclared* debug on an attested app and no reachable state
   where an attested app is debuggable without a verifier seeing it. (A dev-mode app is root-able by
   default — RFC 0026 — and hidden from verification, so the field is meaningless there.)

3. **Binding the declaration to the attestation.** Same two paths as RFC 0025 caps:
   - *Source projects:* `operator_debug` lives in the repo-committed `project.json` inside `files/`,
     so `tree_hash`/`git_tree_sha` commits to it. Pull the source at the hash → the declaration is
     there.
   - *Image projects:* bound by the append-only audit `detail` at promote (inside the TEE) plus the
     pinned `image@sha256`. If RFC 0027's binding quote is in play, the field is part of the manifest
     whose `tree_hash` is carried in `report_data` — the disclosure is hardware-rooted, not the
     daemon's unmeasured word.

4. **verify() facts (RFC 0020).** The evidence bundle's `app` block gains `operator_debug`, and
   `Facts` gains a sibling to `attestation_kind`:
   `operator_debug: { enabled, principals, scope, last_session_at }`. It is a **fact, not a verdict** —
   RFC 0020 renders no green/red. A consumer allowlisting `source.tree_hash` already pins the exact
   config that says debug is enabled; a stricter consumer policy can reject any bundle where
   `operator_debug.enabled` is true. Do **not** collapse `operator_debug: enabled` into "attested" —
   the same honesty line RFC 0027 draws for `daemon-vouched` vs "isolated."

5. **Sessions ride RFC 0026.** A session opens through `POST /_api/projects/<name>/debug` — but here
   it is **granted, not refused**, when the caller is a declared `principal` within `scope`. Every
   open + command is an audit event (`proxy/audit.py`), RTMR-extended like the attested-deploy audit,
   and (with RFC 0027) an `EmitEvent("tee-daemon/debug", {name, principal, scope})` so the session
   shows up in RTMR3's measured log and a later daemon quote. Opening a session does **not** re-measure
   or invalidate the running code — it was already declared possible. `last_session_at`/the audit tail
   let a consumer see *whether* the disclosed door has been used, not just that it exists.

6. **Console (RFC 0016).** The console renders the category as its own rung between dev and locked
   attested: an "attested • operator-debug" badge, the `principals`/`scope`, the last-session time,
   and a lock-down hint ("remove `operator_debug` and re-promote to reach fully-locked attested") —
   matching the dev→attested ladder posture in RFC 0016/0027.

### The ladder
```
dev / debug            root by default, hidden from verifiers, no audit (RFC 0026)
attested + declared    exact code measured; a named operator can attach, audited & disclosed  ← this RFC
attested (locked)      exact code measured; no operator door declared
```
The rungs are honest about *different* claims, not *more* trust — "locked" is not "more attested," it
is a *narrower disclosed capability set*.

## Doesn't debug defeat the point of attestation?
It changes the claim, honestly. Attestation never meant "no human can ever touch this"; it meant
"exactly this code runs." Declared debug keeps that guarantee and adds a second, explicit one: "and
this named operator can attach, audited" — strictly more honest than today's binary, where an operator
who needs debug either avoids attesting or keeps a hidden channel that voids it in fact but not in the
badge. A consumer requiring *no* operator door reads `operator_debug.enabled == false` and picks a
locked app; one fine with an audited operator (most internal/early apps) reads the disclosure and
proceeds. Neither is lied to. What this RFC forbids is the one state RFC 0026's binary quietly
permits: an attested badge over a secretly-reachable app.

## Revocation / downgrade
Removing the door is the same "re-attest from a new measurement" as promotion: drop `operator_debug`
from the manifest and re-promote → new `tree_hash` (or new binding quote) → `verify()` now reports
`operator_debug.enabled == false`. There is no in-place "quietly turn debug off"; the config that
allowed it is measured, so changing it changes the measurement. Revoking a single `principal` is the
same edit. Emergency: tearing the app down removes the surface entirely (RFC 0026's teardown path).

## Relation to other RFCs
- **RFC 0026** — this *revises* its refusal. 0026's "un-promote to debug (voids attestation)" stays
  the right answer for an app that did **not** declare the capability; this RFC adds the declared
  path so an app that expects operator access says so up front and keeps its attestation. Same
  audited-broker session mechanics; different pre-condition (declared principal in scope).
- **RFC 0025** — sibling. `operator_debug` is another elevated capability gated caps⟹attested; this
  RFC is the debug specialization of "elevation forces transparency."
- **RFC 0016 / #16** — not a new *mode* on the visibility×attestation split, but a declared field on
  the attestation axis, surfaced as its own console rung and `verify()` fact.

## Out of Scope
- The session broker itself (RFC 0026) and its TTL/delegation mode.
- The appraisal/verdict layer that would *decide* whether declared debug is acceptable (RFC 0020/0022);
  this RFC only makes the fact legible.
- Scoped per-client daemon tokens for `principals` (issue #18) — assumed as the identity source, not
  built here.
- Closing the shared-isolate gap (RFC 0027); debug scope is per-container, and does not change tenant
  isolation.

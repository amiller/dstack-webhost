# RFC 0026: Operator Debug Access (promotion-gated)

**Status**: Draft

## Summary
Give the operator **root exec / logs / file access into any NON-promoted (dev-mode) project container**,
brokered by the daemon so it does **not** leak into the promoted/attested ones — and **record every access**.
Promoted apps are refused: you **un-promote to debug** (which voids the attestation; re-promote re-attests
from the new measurement). This replaces the privileged `ssh-debug` sidecar. It is **not** a "scoped
capability" — for your own dev apps it's just root; the only thing being scoped is the **promotion boundary**.

## Problem
- **Host SSH = root over EVERY container, including promoted/attested ones**, which silently breaks their
  attestation (they're no longer running only the measured code). That's the `hermes-staging` contradiction:
  a dev-OS box with attested projects *and* an ssh sidecar → its promoted apps are shell-reachable.
- **Prod-OS has no SSH at all.** We removed the sidecar (and de-allowlisted its compose hash on-chain).
- So there's no way to get root into *your own dev app* without either exposing the promoted tenants (SSH)
  or redeploying.

## Design
`POST /_api/projects/<name>/debug` (authed = owner), through the daemon's existing **filtered docker-proxy**:
- **`mode != attested` → granted.** Full exec / interactive shell + `logs` + read `dataDir`, on **that one
  container**. It's your dev app; root is the expectation — no TTL, no capability ceremony.
- **`mode == attested` → refused.** Message: *"un-promote to debug — that voids the attestation; re-promote
  re-attests."* You cannot live-debug a promoted app: `exec`-ing into it means it's no longer only the measured
  code, so "debug it a little" is a contradiction. Attestation is **all-or-nothing on the running code**.
- **Every open + command is an audit event** (`proxy/audit.py`), RTMR-extended like the attested-deploy audit.
  Recording is the point — especially the un-promote→debug of a formerly-attested app (the "operator touched a
  promoted app" moment). You may not always *gate* access, but you always *record* it.

## Why this beats host-SSH
The value is not a leash on the operator — it's that **the daemon enforces the promotion boundary** (root into
non-promoted, hands off promoted) and **records it**, instead of host-SSH's blanket root-over-everything.

## Optional (not core)
A **TTL'd / expiring grant** only earns its keep when *delegating* debug to **someone else** (not the owner).
Keep it as an optional mode; the default (owner → own dev container) is plain root.

## Relation / companion gap
Same "record the event" spine as the **deploy audit** (`deploy.py` records + RTMR-extends updates). Note the
companion gap: today that audit fires **only for attested mode** ("Only record audit log for attested mode"),
so a **dev-app update to a malicious version leaves no trace**. Same fix — *record the event even when you
don't gate it* — applies to both updates and debug. Builds on the per-project `mode` axis (#16) and the
already-existing docker-proxy + audit.

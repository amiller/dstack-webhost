# RFC 0024: Cross-pod federation — code-identity admission + mutual attestation

## Summary
Generalize Nerla Jean-Louis's `OptOutAppAuth` (update governance with fine-grained
per-user opt-out, `njeans/dstack` `update-demo`) from *"which **versions** may touch
this data"* to *"which **code-identities** may touch this data"* — where a code-identity
is a compose hash, whether it is a **successor version** or a **peer pod's app**. With
that one generalization, three operations become the *same* governed action: **upgrading**,
**federating with a peer**, and **admitting a visiting ambassador**. All go through the
same governance + timelock + per-user opt-out; the KMS gates key release on the same
contract policy. Add (a) a **mutual-attestation handshake** so two pods each verify the
other's code-identity before data flows, and (b) **two read modes** — *in-place
ambassador* (compute-to-data) and *re-encrypted share* (data-to-compute).

(The IAppAuth-extension framing is Andrew's; Nerla's demo is the specific per-user
opt-out instantiation this builds on.)

## Problem
- **dstack apps are single-lineage today.** An `AppAuth` authorizes *versions* for one
  `appId`. Two pods running identical code (same compose hash, different `appId`) have no
  defined trust path to exchange data — "you're running the code I expect → here's data"
  needs a bespoke handshake every time.
- **Nerla's `OptOutAppAuth` solves only the upgrade case.** It is sequential (v1 → v2,
  one-shot migration, v1 retires) and single-operator. It does not cover *concurrent*
  peers (live federation, an ambassador reading alongside the host) or the *mutual*
  attestation a cross-operator relationship requires.
- Without a named pattern, every federation re-derives an ad-hoc exfiltration/trust model.

## What Nerla's demo already gives us (the base to generalize)
`OptOutAppAuth.sol` (implements `IAppAuth`):
- `allowedVersions[composeHash]` — which versions the KMS will release data keys to;
  `addVersion`/`removeVersion` gated `onlyOwner = daoContract` (OZGovernor + Timelock).
- `optOut()` — any user self-records onto a public `optOutUsers[]`.
- `isAppAllowed(bootInfo)` — the KMS gate (keys only to permitted `(appId, version)`).

Migration enforcement (the app, attested): v2 mints a fresh migration keypair **from the
same key-provider**; v1 **verifies v2's key came from that KMS** (cert-chain) and **reads
the on-chain opt-out list**, then **re-encrypts every non-opted-out user's data to v2's
key** — opted-out data is *left behind*, unreadable by v2. Trust = v1 (attested) honors
the on-chain list + the same-KMS check. ("Encrypted to a contract policy," not to a raw
hash — the policy can encode governance + opt-out.)

## Design

### 1. Code-identity admission policy (generalize OptOutAppAuth)
- `allowedVersions[hash]` → **`allowedCodeIdentities[hash]`**: the compose hashes the
  KMS will release this data's keys to / that data may be re-encrypted toward. A successor
  version and a peer pod's app are both just entries.
- `isAppAllowed` unchanged in spirit (KMS gate). Admitting an identity = governance
  (vote + timelock), as today. `optOut()` per-user, unchanged — now opts out of *any*
  admission, not only upgrades.
- **Upgrade, federate-with-peer, admit-ambassador collapse into one governed operation:**
  "admit a code-identity to the data policy."

### 2. Mutual-attestation handshake (the cross-operator new part)
- Before data flows between pods A and B, each runs **RFC 0020 `verify()`** on the other
  and checks the peer's compose hash is in its admitted set; an RA-TLS channel binds the
  session to both quotes.
- Resolves the asymmetric trust: the **host verifies the visitor** (knows what it will do
  with the data); the **visitor verifies the host** (its inputs/secrets won't be stolen).
- The **canonical "this hash is the real app X" reference** = the admitted set on the
  on-chain policy, optionally plus a curator endorsement (**RFC 0022**) — so a look-alike
  compose hash is not silently accepted.

### 3. Two read modes (both requested)
- **Mode A — in-place ambassador (compute-to-data).** The host admits the visitor's
  compose hash as a code-identity allowed to run *against the data inside the host room*.
  The visitor's agent executes in the host's TEE under an **egress lock** (returns only
  sanctioned output; raw data never leaves). **Strongest containment**; the host must be
  willing to run the visitor's attested code.
- **Mode B — re-encrypted share (data-to-compute).** The host re-encrypts non-opted-out
  data to the **peer's KMS-verified key** — Nerla's mechanism, but to a *concurrent peer*
  and **ongoing/per-grant** rather than one-shot to a successor. The peer reads its own
  copy. Data moves → the **opt-out must hold across both rooms** (admit only peer
  code-identities whose policy honors the same opt-out). Closer to TinyCloud
  proxy-re-encryption sharing.
- Both honor the per-user opt-out **at admit time** (A: opted-out data not exposed to the
  visiting agent; B: not re-encrypted to the peer).

### 4. Sequential vs concurrent data plane
The *policy* is shared (admit a code-identity); the *data plane* differs:
- **Sequential** (version succession) — migrate-then-retire, one-shot (Nerla's demo).
- **Concurrent** (live peer/ambassador) — Mode A runs the visitor in-place; Mode B does
  ongoing selective re-encryption. The contract layer is identical; only the app's
  data-movement differs.

## Files / prior art to build on
- **Nerla's `OptOutAppAuth.sol` + demo migration** (`njeans/dstack` `update-demo`) —
  generalize `allowedVersions` → `allowedCodeIdentities`; make re-encryption ongoing for
  Mode B; reuse the same-KMS-key verification + on-chain opt-out read verbatim.
- **`08-extending-appauth` timelock** (proposeComposeHash → notice → exit) — compose with
  opt-out: notice period *and* per-user decline.
- **dstack KMS / `IAppAuth`** (`kms/auth-eth`) — the key gate.
- **RFC 0017** (state durability across CVM lifecycle) — the data-room state model that
  binds data to the policy and survives the admit/migrate events.
- **RFC 0020** (`verify()` → facts) — the handshake's attestation check; **RFC 0022**
  (appraisal) — the "hash X is the real app Y" endorsement.

## Out of scope / deferred
- The ambassador's **app-level identity and attenuation** — `oauth3-server/rfcs/0006`
  (ambassador = a cross-operator delegation on its RFC 0003 continuum).
- **Cross-pod revocation propagation** — revoke an admitted identity → both data planes
  must stop. Hard for Mode B (copies already shared); needs explicit treatment.
- Economic / anti-sybil controls on federation membership.

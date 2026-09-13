# RFC 0032: Capability storage module for webhost apps

**Status**: Ready for implementation (kanban 0032-1..5; start at 0031-1)

## Summary

Give webhost apps a standard **capability-storage module**: path-scoped, link-issuable
read/write capabilities over pod-owned files, built on permissive primitives instead of
tinycloud-node's EGPL stack. Two token types cover the useful rungs of the identity
ladder — **Biscuit** (Apache-2.0) for bearer links with offline holder attenuation, and
**ucanto** (Apache/MIT) for identity-bound standing delegations — behind one file store
and one authorization surface. An extension point adds per-use **TEE co-signing** for
sensitive operations via Biscuit third-party blocks.

Grounded in the 2026-07-10 bake-off: both arms are already live
(`/capdemo` tinycloud-backed, `/caps-server` Biscuit at 173 lines), seven systems were
evaluated hands-on (`~/projects/storage-bakeoff/landscape/`), and the full report is at
`pod.dstack.soc1024.com/bakeoff-report/`.

## Problem

- Pods have no data-sharing primitive. Every app that wants "give this person a link to
  read this file" rolls its own auth or reaches for a platform (tinycloud-node), whose
  EGPL gates production use and non-"aligned" deployment.
- The bake-off showed the delegation semantics are cheap to own: path/file/section-scoped
  read-write links in ~173 Apache-licensed lines; identity-bound UCAN chains with a
  revocation hook in one pure-JS container. ~85% of tinycloud's auth core is still-Apache
  Kepler code; the features we need predate the relicense.
- What no single permissive primitive provides is the *server around the token*: storage,
  issuance/claim UX, revocation state, grant inventory, and multi-tenant identity. That's
  the module this RFC specifies, so apps get it once instead of ad hoc.

## Design

1. **One file store, two verifiers.** An app-local module (library first; optionally a
   shared `caps` service app later) owning a volume-backed file tree, authorizing each request by
   whichever credential arrives:
   - **Biscuit token** (rung 1 — bearer link): path-prefix + operation + expiry caveats,
     ~470 B, offline-verifiable against the module's root public key. Recipients can
     attenuate offline (folder-rw → file-ro) with no server round-trip; re-widening is
     cryptographically blocked.
   - **ucanto invocation** (rung 2 — identity-bound): UCAN delegation chains to a DID;
     invocations are signed, so wrong-key replay fails and over-delegation is caught at
     every hop. For agents and SDK-style integrations holding standing access.
2. **Links.** `claim/<id>` URLs as in the live demos. Biscuit links carry the token
   (bearer). ucanto links embed a throwaway keypair + delegation (22-line pattern) when
   zero-onboarding is wanted; standing grants delegate to the recipient's existing DID.
3. **Revocation + inventory.** sqlite table of revoked Biscuit block-ids (revoking a root
   block kills its whole attenuation subtree) and revoked ucanto delegation CIDs (checked
   in `validateAuthorization`). All issuance is logged, giving the operator the rung-4
   property token systems lack: enumerate outstanding grants.
4. **Rung-3 extension: TEE co-signer.** Sensitive operations mint tokens carrying
   `check if approved($p) trusting <TEE pubkey>`; an attested approver app signs a
   resource-bound approval per use (verified fail-closed in the bake-off). This is the
   OAuth3 thesis in capability form and composes with RFC 0024 mutual attestation and
   RFC 0025's caps⟹attested rule.
5. **Identity/recovery rule.** Durable authority lives with a *recoverable* root — the
   operator's dstack-KMS-derived key or an email-attested account — which mints and
   revokes disposable agent/device keys. Never bake a raw user key into a namespace
   (tinycloud's space-name-is-the-key design has no rotation story; Storacha's
   `did:mailto` root is the model to follow).

## Non-goals

- The whole-platform SDK surface (VFS, SQL, hooks, client encryption) — apps that want
  that breadth should weigh tinycloud's EGPL terms deliberately.
- E2EE at rest (capability = decryption key). That's Peergos' model; if it becomes a
  requirement, evaluate promoting the Peergos arm instead of bolting crypto onto this.
- tinycloud wire-protocol interop.

## Alternatives considered

- **tinycloud-node as the module**: right shape, proven on prod, but EGPL production
  gating, no revocation, protocol-version lockstep, and the unrecoverable-root identity
  flaw. Remains viable if the licensing ask (Additional Use Grant / MIT on node+SDK) lands.
- **OpenFGA**: instant revocation + grant inventory in one sqlite container, but
  server-resident only — loses offline links and attenuation. Could later back the
  inventory/audit side of this module.
- **Kepler fork**: Apache ancestor builds and boots; 3–5 days to a modernized fork.
  Kept as the negotiation BATNA rather than the build path.

## Library API contract (0031-1/2/3 build to this)

```ts
createCaps({ dataDir, rootKey, db }): Caps        // rootKey: biscuit "ed25519-private/…" serialization
caps.mint({ path, ops, expiry }): { token, id }   // ops ⊆ ["read","write"]; path is a prefix
caps.verify(credential, { path, op }): { ok } | { ok:false, code }   // credential = Biscuit token (0031-1) or ucanto invocation (0031-3)
caps.read(credential, path) / caps.write(credential, path, bytes)    // verify + file I/O
caps.revoke(id): void                             // 0031-2: kills token AND its attenuated descendants
caps.grants(): Grant[]                            // 0031-2: issuance log incl. revoked flag
```

Reference implementations to port (do not redesign): `storage-bakeoff/caps-server/server.mjs`
(Biscuit mint/verify — `authorizeWithLimits` is REQUIRED or first calls time out, and run
node with `--experimental-wasm-modules`; bun cannot load the wasm) and
`storage-bakeoff/landscape/ucanto/{lib,exp1-2,exp3-link,exp4-revocation}.mjs`
(identity-bound arm; pure JS).

## Acceptance (E2E, scriptable; each numbered item is a hard pass/fail)

Run against the library exercised through a demo HTTP server (the caps-server UI is fine):

1. roundtrip: `mint({path:"repo/", ops:[read,write]})` → write → read → bytes equal.
2. link-claim read: a read-only token for `repo/A/` reads `repo/A/x` — via the token alone.
3. attenuation refusal: that token's write to `repo/A/x` → `{ok:false}` AND HTTP 403.
4. scope refusal: read of `repo/B/y` with the `repo/A/` token → refused.
5. section scoping: token scoped to `repo/F/s2-…/` reads that section, cannot read `repo/F/_full`.
6. wrong-key/stranger (0031-3): replaying a delegation with a different signing key → refused.
7. expiry: token minted with a past expiry → refused.
8. revocation (0031-2): after `revoke(id)`, check 2 flips ok→refused; a HOLDER-ATTENUATED
   descendant of the revoked token is also refused.
9. inventory (0031-2): `grants()` lists every mint from 1–8 with path/ops/expiry/revoked.
10. persistence: restart the process; 8 and 9 still hold (sqlite-backed).

0031-4 packages 1–10 as an exit-code script runnable in CI and against a staging
deployment. Timings are informational, not gated.

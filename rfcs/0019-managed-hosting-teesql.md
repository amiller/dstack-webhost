# RFC 0019: Managed Hosting via TeeSQL / AttestMesh

## Summary
Run dstack-webhost as a managed, multi-tenant service on infrastructure TeeSQL
operates (their AttestMesh mesh and dstack nodes), so the hosting/ops burden moves
to them while we keep the app and the attestation-appraisal layer. dstack-webhost
stays open and self-hostable for personal use; the managed deployment is the paid,
multi-tenant version. This is a request for collaboration, not a committed design.

## Context
Two gaps line up:

- **Ours.** dstack-webhost is a single-CVM host. Durability and fleet recovery are
  shallow (RFC 0017), there is no HA, and operating it reliably enough to sell to
  others is work we don't want to own. RFCs 0016 / 0017 / 0018 describe the
  reliability surface; we'd rather have it operated than operate it.
- **Theirs.** AttestMesh is a live mesh coordination layer (Base mainnet,
  on-chain-anchored membership and attestation) with no real application running on
  it. It needs a flagship workload.

A multi-tenant attestable-app host is a genuine stateful-cluster use of the mesh
(membership, the cluster shared key, peer coordination), not a toy. Putting
dstack-webhost on AttestMesh gives them their first real app and gives us a managed,
reliable substrate.

## What we bring
- **dstack-webhost** — the multi-tenant app host (deploy via git/API, dev →
  attested-promotion, per-tenant isolation), unchanged for self-host and adapted for
  the managed/clustered deployment.
- **Attestation appraisal** — independent validation and explanation of each hosted
  app's attestation path, distilled from existing research practice into a repeatable
  workflow. This is the differentiated value the managed service sells on top of raw
  hosting; its audience is the customer's customer (the relying party).

## What we're asking TeeSQL for
1. **Operate the metal.** Run and keep up the dstack nodes and the AttestMesh mesh on
   an on-chain-anchored ecosystem (base-prod — see Constraints). Own deploys,
   upgrades, health, and recovery.
2. **Provide the reliability primitives we'd otherwise build.** Same-app_id replicas
   behind the gateway's existing health-routing for HA; durable per-tenant state that
   survives CVM recreation (the externalized-volume / pinned-restore shape of RFC
   0017, or in-mesh shared state keyed by the CSK — see Options).
3. **Let the appraisal lean on AttestMesh's attestation machinery.** The on-chain KMS
   sig-chain verification, the attested indexer, and its RPC-repro-stub pattern
   already cover much of what an attestation appraisal needs as a verification
   backend. We'd build the appraisal on top rather than re-implement it.

## Product shape
- **Managed multi-tenant service** — dstack-webhost on TeeSQL-operated AttestMesh,
  that others sign up for, with the attestation appraisal per hosted app.
- **Self-hostable** — dstack-webhost stays open and easy to run on your own dstack
  box, as today. Open-core: same codebase, two deployment modes.

## Architecture options (decide together)
- **(a) Managed dstack nodes, mesh unused.** TeeSQL runs dstack nodes; we deploy
  dstack-webhost onto them as ordinary apps. Simplest, fastest start; the mesh is not
  load-bearing.
- **(b) dstack-webhost as a real AttestMesh cluster.** Multiple webhost members in an
  AttestMesh cluster, per-tenant durable state keyed by the CSK, mesh for
  coordination. More work, but it's the stronger joint story (their mesh actually
  carries the app) and gives HA + shared state natively.

Recommendation: start with (a) to get live, with (b) as the target once it's a real
product. Their input decides this, since they operate it.

## Working model (open)
- Whether this is a flat managed-infra fee, a revenue share on the service, or a
  deeper partnership.
- Who owns the tenant relationship, billing, and signups (default: we do; TeeSQL is
  invisible infra).
- Whether the appraisal is co-branded or ours alone.

## Open questions
- (a) vs (b) above.
- Which base-prod ecosystem, and whether our existing creds/accounts reach it.
- How much of the appraisal AttestMesh's primitives cover vs. we build.
- Operational expectations for "reliable": recovery time, replica count, SLA.

## Out of scope
- The internal design of the durable-state layer (RFC 0017 / a follow-up).
- The appraisal report-card schema (separate spec).
- Any change to the self-host path's simplicity.

## Constraints
- **base-prod only.** The on-chain attestation story (verifiable allowlist, gateway
  anchored on Base mainnet) requires a base-prod ecosystem. pha-prod returns
  chain_id 0 / no KMS contract and is a dev placeholder; the managed service must not
  run there.

# RFC 0019: Managed Hosting via TeeSQL / AttestMesh

## Summary
Run dstack-webhost as a managed, multi-app service on infrastructure TeeSQL
operates (their AttestMesh mesh and dstack nodes), so the hosting/ops burden moves
to them while we keep the app and the attestation-appraisal layer. dstack-webhost
stays open and self-hostable for personal use; the managed deployment is the paid,
multi-app version. This is a request for collaboration: the design below is
reviewable, but the (a)/(b) topology choice and the commercial model are decided
with TeeSQL, not unilaterally here.

## Motivation
Two gaps line up:

- **Ours.** dstack-webhost is a single-CVM host. Durability and fleet recovery are
  shallow (RFC 0017), there is no HA, and operating it reliably enough to sell to
  others is work we don't want to own. RFCs 0016 / 0017 / 0018 describe the
  reliability surface; we'd rather have it operated than operate it.
- **Theirs.** AttestMesh is a live mesh coordination layer (Base mainnet,
  on-chain-anchored membership and attestation) with no real application running on
  it. It needs a flagship workload.

A multi-app attestable host is a genuine stateful-cluster use of the mesh
(membership, the cluster shared key, peer coordination), not a toy. Putting
dstack-webhost on AttestMesh gives them their first real app and gives us a managed,
reliable substrate.

What each side brings:

- **We bring dstack-webhost** — the multi-app host (deploy via git/API, dev →
  attested-promotion, per-app isolation), unchanged for self-host and adapted for
  the managed/clustered deployment — and the **attestation appraisal**: independent
  validation and explanation of each hosted app's attestation path, distilled from
  existing research practice into a repeatable workflow. The appraisal is the
  differentiated value the managed service sells on top of raw hosting; its audience
  is the customer's customer (the relying party).
- **We ask TeeSQL to operate the metal** — run and keep up the dstack nodes and the
  mesh on an on-chain-anchored ecosystem (base-prod; see Constraints), owning
  deploys, upgrades, health, and recovery — **to provide the reliability primitives
  we'd otherwise build** (same-`app_id` replicas behind the gateway's existing
  health-routing for HA; durable per-app state that survives CVM recreation), and
  **to let the appraisal lean on AttestMesh's attestation machinery** (the on-chain
  KMS sig-chain verification, the attested indexer, its RPC-repro-stub pattern)
  rather than re-implementing it.

## Design

### TeeSQL data model
The unit of deployment is a dstack-webhost instance (one daemon) owning N projects. The
atom is the `Project` manifest (`proxy/projects.py`): `name`, `runtime`, `entry`,
`port`, `mode` (dev/attested), the source pins (`source`/`ref`/`commit_sha`/
`tree_hash`, `image`/`image_digest`), `volumes`, and the per-app attestation fields
(`app_id`, `app_pubkey`, `binding_quote`, `attestation_kind`). Private app state
lives in the per-project data volume (`/daemon-data/<name>`); secrets live in `env`
and are the credential broker's problem (RFC 0018), not the host store's.

- **Self-host (today):** manifests on the daemon volume, code pinned by
  commit/tree_hash, everything CVM-local. Single-CVM blast radius; RFC 0017's
  external named volumes plus export/import are the durability floor.
- **Managed, option (a):** the identical model on a TeeSQL-operated node; no state
  moves and no new store exists.
- **Managed, option (b):** the manifest registry becomes mesh-shared state keyed by
  the cluster shared key (CSK) — membership-encrypted replication across webhost
  members — and per-app `dataDir`s are sealed blobs restorable only inside the
  same app identity. RFC 0017's export bundle is the canonical serialization in
  transit and the bootstrap artifact when a member joins: a new member restores from
  the bundle during `recover_all()` before it serves traffic.

What TeeSQL holds vs. never holds: they operate nodes, volumes, and the mesh, but no
plaintext app secret ever reaches them — `env` stays behind RFC 0018 grants /
dstack KMS-derived keys — and manifests plus pins are public-safe metadata (the RFC
0015 verification surface already publishes them for attested projects).

### AttestMesh topology
- **Edge:** the gateway, unchanged — health-routed, `zt-cert` quote pinned; this is
  the channel layer an appraisal consumer checks first.
- **Members:** dstack-webhost daemons as AttestMesh cluster members. HA is
  same-`app_id` replicas behind the gateway's existing health routing; coordination
  (which member owns a project's container, membership churn) runs over the mesh
  under the CSK.
- **Anchoring:** membership and attestation are anchored on-chain on Base mainnet
  (base-prod). The on-chain KMS sig-chain verification and the attested indexer
  serve as the verification backend for the mesh itself, and our appraisal layer
  builds on them — the indexer's repro-stub pattern is the same posture RFC 0020
  requires of evidence consumers, so we extend rather than duplicate.
- **Granularity:** a shared instance is daemon-vouched (`attestation_kind:
  "daemon-vouched"` — the hardware quote attests the webhost, which vouches project
  → tree_hash); an app that needs a hardware quote of itself gets a
  per-app CVM (`"app-cvm"`). Granularity is a per-app deployment choice, not a
  platform fork.

### Architecture options (decide together)
- **(a) Managed dstack nodes, mesh unused.** TeeSQL runs dstack nodes; we deploy
  dstack-webhost onto them as ordinary apps. Simplest, fastest start; the mesh is
  not load-bearing.
- **(b) dstack-webhost as a real AttestMesh cluster.** Multiple webhost members in
  one cluster, per-app durable state keyed by the CSK, mesh for coordination.
  More work, but the stronger joint story (their mesh actually carries the app) and
  HA + shared state natively.

Recommendation: start with (a) to get live, with (b) as the target once it's a real
product. Their input decides this, since they operate it.

## Deployment story
One codebase, two deployment modes (open core):

- **Self-host** stays exactly as today: a single CVM, `docker-compose.yaml`, no
  mesh, no TeeSQL. Any change that complicates the self-host path is out of scope.
- **Managed bring-up (option a):**
  1. TeeSQL stands up dstack node(s) on a base-prod ecosystem.
  2. dstack-webhost itself is deployed there as an *attested* app — promoted through
     the same dev → attested gate as any other app, `app_id` from the on-chain
     `DstackApp` allowlist, binding quote recorded (the RFC 0025 machinery,
     unchanged).
  3. External named volumes are wired per RFC 0017's operational requirement, and
     export/import is enabled — this is the whole recovery story until (b) exists.
  4. Apps onboard through the existing `/_api` deploy + promotion; nothing in the
     app path differs between modes.
  5. Upgrades: a new pinned webhost image, deployed and re-promoted like any other
     version — the pin is the upgrade contract; no in-place mutation.
  6. Recovery: RFC 0017 pinned restore — fresh CVM, import bundle, `recover_all()`
     rebuilds the fleet at recorded pins, refusing (never re-cloning latest) on pin
     mismatch. In (b) the same restore runs mesh-side keyed by the CSK.

**Ownership:** we own the customer relationship, billing, and signups by default;
TeeSQL is invisible infra. The appraisal is ours; co-branding is an open question.

**Migration (a) → (b):** the RFC 0017 export bundle is carried across verbatim — it
is the compatibility contract between the two options, so no bespoke migration
format ever exists.

## Open Questions
- (a) vs. (b) — and if (b), the ownership/coordination protocol details on the mesh.
- Which base-prod ecosystem, and whether our existing creds/accounts reach it.
- How much of the appraisal AttestMesh's primitives cover vs. we build.
- Operational expectations for "reliable": recovery time, replica count, SLA.
- Commercial model: flat managed-infra fee, revenue share on the service, or a
  deeper partnership.
- Whether the appraisal is co-branded or ours alone.

## Out of scope
- The internal design of the durable-state layer (RFC 0017 / a follow-up).
- The appraisal report-card schema (separate spec).
- Any change to the self-host path's simplicity.

## Constraints
- **base-prod only.** The on-chain attestation story (verifiable allowlist, gateway
  anchored on Base mainnet) requires a base-prod ecosystem. pha-prod returns
  chain_id 0 / no KMS contract and is a dev placeholder; the managed service must not
  run there.

## MVP slice
First implementable step: **option (a) pilot — one managed base-prod dstack node,
mesh unused, durability floor wired.** TeeSQL operates the node in production; any
base-prod dstack node qualifies to prove the slice (a self-operated one stands in
until theirs exists).

Our side, implementable now:
1. RFC 0017 export/import plus the external-named-volume requirement land — this is
   the pilot's recovery path (prerequisite, tracked under RFC 0017).
2. Deploy dstack-webhost as an attested app on that node, promoted through the
   normal gate, so the hosting layer is itself attestable — the managed pitch's
   proof point.
3. Document the managed bring-up runbook (the steps above) in-repo; credentials
   stay out of the repo entirely.

Acceptance criteria for the slice:
- The managed host serves `GET /_api/version` pinned to this repo's commit, and its
  public RFC 0015 endpoints (`/_api/attest/<name>`, `/_api/verification/<name>`)
  return a valid binding (`app_id`, `tree_hash`, quote) for the daemon's own
  attested deployment.
- One app deployed and promoted through `/_api` on the managed host verifies
  end-to-end via the same public endpoints.
- Export → destroy → import on the managed host restores the fleet at identical
  pins; a tampered pin errors, never silently re-clones.
- No mesh dependency: the pilot runs with AttestMesh absent — that is the definition
  of option (a).

The option (b) cluster work (CSK-keyed shared state, mesh coordination, HA
replicas) is a follow-up tracking issue filed against this section, not part of the
slice.

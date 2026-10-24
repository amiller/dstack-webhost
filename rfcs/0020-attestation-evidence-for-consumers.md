# RFC 0020: Machine-Verifiable Attestation Evidence for App Consumers

## Summary
Make a hosted app's attestation evidence verifiable by the app's *consumers* —
agents, counterparty services, contracts — not by a human looking at a badge. Serve
a stable evidence bundle per app and ship a `verify()` library that returns
structured **facts**: a verified binding from a live endpoint to the exact source
tree running in a TEE. The library renders no verdict and shows no green/red; the
accept-or-reject policy lives entirely in the consumer.

## Problem
Attested promotion already binds a project's source-tree hash and exposes a
verification endpoint (RFC 0015, `proxy/ingress.py`), and `verify.md` renders a
human "is it verified" view. Three things are wrong with stopping there:

- **The human is never the verifier.** An end user clicks a friend's app link
  regardless of any badge. The thing that actually consumes an attestation and makes
  a decision with it is software at a trust boundary: an agent about to send a user's
  data to the app or act on its output, a counterparty service integrating with it, a
  contract about to release something to the endpoint.
- **A rendered verdict bakes in a policy nobody agreed to.** "Verified ✓" presumes
  one universal definition of acceptable, served by the thing being evaluated. There
  is no universal "safe" — acceptability depends on what the relying party requires.
- **The evidence is not assembled into one channel-bound chain a program can check.**
  The four layers live in different places, so there is no clean path for a consumer's
  code to go from "this URL answered me" to "I hold a verified binding to exactly this
  source, running approved platform code, in a TEE, reached over an attested channel."

The four layers to assemble:
1. **Channel** — reached over an attested gateway (the gateway's `zt-cert` quote, pinned).
2. **Platform** — the webhost `app_id`'s on-chain `DstackApp` allowlist on base-prod.
3. **Per-app binding** — the source-tree hash, vouched by the daemon's promotion quote.
4. **Source** — inspectable at that hash.

Note on (3): in a shared instance the TDX quote attests the *webhost daemon*, not each
app. The daemon vouches "project P = tree_hash H." A consumer's trust is therefore
hardware quote → approved webhost code → its signed binding → source. A consumer that
needs a hardware quote of the *app itself* needs a per-app CVM — a deployment-granularity
choice (RFC 0019), out of scope here.

## Files to Modify
- `proxy/ingress.py` — extend the RFC 0015 verification endpoint to serve the full
  evidence bundle (below) in a versioned schema, including the on-chain and gateway
  reference pointers.
- `proxy/deploy.py` — ensure the promotion record carries everything the bundle needs
  (`source` repo/ref/`commit_sha`/`tree_hash`, `image_digest`, and the quote that
  binds the hash).
- `verify/` (new) — the `verify()` library. Target a small TS package agents/services
  embed; a thin Python helper for the daemon's own use. Returns facts, not a verdict.
- `verify.md` — repurpose as a human-readable *renderer of the same facts* for
  dev/debug, with an explicit banner that it is **not** a verification mechanism for
  end users.
- `docker-compose.yaml` / config — the base-prod RPC and the pinned gateway/KMS
  reference values the bundle cites.

## Implementation
1. **Evidence bundle**, served at the verification endpoint:
   `{ schema_version, platform_quote, webhost_app_id, onchain { chain_id,
   kms_contract, dstackapp, allowed_compose_hash, allowed_os_image }, gateway {
   domain, app_id, zt_cert_ref }, app { project, source { repo, ref, commit_sha,
   tree_hash }, image_digest, binding_quote } }`. Every value is evidence plus a repro
   pointer (the on-chain call, the quote field) so the consumer re-checks it against
   Base / Phala / Intel PCS directly. Authority comes from the evidence, not from us —
   the same posture as the AttestMesh indexer's repro stubs.
2. **`verify(endpoint, opts) -> Facts`.** Fetches the bundle; verifies the TDX quote
   (DCAP/QVL + collateral); confirms the gateway `zt-cert` binding; confirms
   `webhost_app_id` is on-chain-approved on base-prod; confirms the project's
   `tree_hash` is bound by the daemon's quote. Returns
   `{ quote_valid, kms_root, webhost_app_id, onchain_approved, gateway_attested,
   source: { repo, commit_sha, tree_hash }, errors[] }`. **No accept/reject.**
3. **Policy lives in the consumer.** Ship two *reference* policies as examples, not as
   the library's job: (a) allowlist match on `source.tree_hash`; (b) a minimum of
   `quote_valid && onchain_approved && gateway_attested && chain_id == <base-prod>`.
   Document the third, strongest consumer step the binding makes possible: pull the
   source at `tree_hash` and reason about it — an agent inspecting the exact code it is
   about to trust.
4. **Channel prerequisite.** `verify()` either performs the gateway `zt-cert`
   verification itself or consumes a pinned base-prod gateway reference (app_id +
   compose_hash) so layer (1) is checked, not assumed.
5. **`verify.md` becomes a Facts renderer** for developers, behind the "not an
   end-user trust mechanism" banner.

## Testing & Validation Requirements
- Fetch the bundle for a promoted project; `verify()` returns `quote_valid: true`, the
  correct `tree_hash`, `onchain_approved: true` on base-prod.
- Tamper the bundle's `tree_hash` → the mismatch appears in `errors[]`/facts; the
  library still returns facts (it does not throw a verdict), and reference policy (a)
  rejects.
- Run `verify()` against a deployment on a non-anchored ecosystem (pha-prod) →
  `onchain_approved: false` / `chain_id: 0` surfaced as facts, not a crash.
- Two different consumer policies accept vs. reject the *same* facts — demonstrating
  there is no universal verdict.
- Agent path: given facts, pull the source at `tree_hash` and confirm it matches the
  repo at `commit_sha`.

## Report Requirements
- A sample evidence bundle (JSON).
- A transcript of `verify()` returning facts for a good deployment and for a tampered
  one.
- The two-policies-one-facts example, showing accept and reject from identical evidence.

## Out of Scope
- The appraisal / opinionated-judgment layer — that is one policy on top of these
  facts, for humans who want a verdict (separate later RFC).
- Per-app hardware quotes / per-app CVMs (deployment granularity, RFC 0019).
- Any human "is it safe" UI as a *trust* mechanism (`verify.md` is dev-only).
- Secret/credential delegation (RFC 0018).

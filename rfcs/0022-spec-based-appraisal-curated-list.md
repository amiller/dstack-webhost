# RFC 0022: Spec-Based Appraisal and Curated App List

## Summary
Add an explicit appraisal layer on top of RFC 0020 facts: a curator publishes a small
declarative spec, an evaluator judges an attested app's facts plus source at `tree_hash`
against that spec, and the result is a per-clause verdict. The verdict gates a curated
homepage/listing view and supplies the failing clauses that RFC 0021 apps spend against.

This does not change RFC 0020's rule that `verify()` returns facts, not a verdict. The
curated list is one named curator's policy: "apps that pass curator C's spec S, judged by
evaluator E." The spec, evaluator version, inputs, and verdict are published so a consumer
can adopt that curator, swap in a different one, or ignore the curation entirely.

## Problem
RFC 0020 deliberately stops before appraisal. It proves a binding from endpoint to platform
quote, on-chain approval, source repo/commit/`tree_hash`, and daemon binding quote, then
returns facts. That is correct for software consumers, but it leaves two product gaps:

- **Humans still need an opinionated surface.** `proxy/ingress.py` currently lists every
  anonymous-visible project whose `mode == "attested"`; `proxy/templates/index.html` renders
  an uncurated grid. "Attested" only means the facts are available, not that the app satisfies
  the safety claim a human expects.
- **Self-improvement has no external target.** RFC 0021 lets an app spend tokens to improve
  evidence and handler legibility, but the daemon does not define the failing clause that says
  what to improve next.
- **The small-audit promise is still manual.** `index.md` says the substrate is learned once
  and each per-app audit reduces to "does this small handler hold the invariant it claims?"
  This RFC makes that per-app audit a reproducible evaluator run against a declared spec.

The hard constraint is RFC 0020's "no universal safe." A curated verdict must not pretend to
be a platform truth. It is a named curator's explicit policy over 0020 facts and source.
Multiple curators and multiple specs are expected.

## Files to Modify
- `verify/` (RFC 0020) — add an appraisal package/CLI that consumes `Facts`, a spec, and the
  source tree at `Facts.source.tree_hash`; emits per-clause verdicts. It must not change
  `verify()`'s return contract.
- New `appraisal/` or `curation/` convention — published curator specs, evaluator metadata,
  verdict JSON, and repro instructions for rerunning the judgment.
- `proxy/deploy.py` — carry an optional app-declared spec pointer or spec file in the promotion
  record, bound at the same `tree_hash` as the app source.
- `proxy/ingress.py` — serve verdicts next to the RFC 0020 evidence bundle and add a curated
  listing endpoint/view on top of the raw attested list.
- `proxy/templates/index.html` — render the curated list by default, with badges for passing
  clauses and a link to the full verdict; keep a raw attested view discoverable for debugging.
- `proxy/audit.py` — record appraisal submissions and curator verdict refreshes.

## Implementation
1. **Spec language.** A spec is a small declarative set of clauses, not prose. Each clause has
   `{ id, claim, evidence_required, severity }`, where `evidence_required` names what the
   evaluator must inspect: 0020 facts, RFC 0021 evidence artifacts, source files at
   `tree_hash`, or known substrate files. Example clauses grounded in this repo:
   - `broker.socket_only`: handler never receives raw `docker.sock`; broker access is through
     the filtered broker mount (`proxy/runtimes.py`, commit `ddb088f7`).
   - `secrets.broker_only`: raw upstream secrets are absent from handler env; secrets flow
     through RFC 0018 broker grants.
   - `manifest.public_env_redacted`: public project manifests redact env values
     (`proxy/ingress.py`, commit `1bb89b7f`).
   - `crypto.constant_time_compare`: secret comparison paths use constant-time primitives; the
     timing-leak examples are negative fixtures (`examples/timing-leak-demo`,
     `examples/rsa-timing-demo`).
   - `substrate.oci_runtime_corroborated`: the app corroborates the daemon's OCI-runtime claim
     using tenant-visible evidence (`examples/isolation-probe`, `/_api/substrate`).
2. **Evaluator input.** The evaluator starts from RFC 0020 facts, fetches the source at
   `Facts.source.tree_hash`, and reads only the app handler, declared evidence artifacts, and
   the substrate references named by the spec. This is the automated form of the small per-app
   audit promised in `index.md`.
3. **Evaluator output.** Emit
   `{ curator, spec_id, spec_hash, evaluator_id, evaluator_hash, facts_hash, source_tree_hash,
   verdict: pass|fail|needs_review, clauses[] }`. Each clause result is
   `{ id, result, evidence: [{ path, line, note }], missing, remediation_hint }`. A top-level
   verdict is derived only from the named spec's clause rules; consumers should be able to
   ignore it and read the clause facts directly.
4. **Attested vs. consumer-run judge.** The first implementation may be consumer-run policy:
   publish the spec, evaluator prompt/code/version, input hashes, and output so anyone can rerun
   it. A stronger deployment runs the evaluator itself as an attested app and publishes its own
   RFC 0020 evidence bundle, making "curator C using evaluator E" reproducible through the same
   mechanism as other apps. The daemon should model both by recording evaluator identity and
   hashes, not by trusting an opaque verdict string.
5. **Curated list.** Add a curated view over the existing raw attested list. The public homepage
   shows apps whose latest verdict passes curator C's active spec S, with per-clause badges and
   a link to the full verdict. The raw `mode == "attested"` JSON/list remains available because
   attestation evidence is still independent of curation.
6. **Feedback edge into RFC 0021.** Failed clauses become spend targets:
   `target = { spec_id, clause_id, current_evidence, remediation_hint }`. An app spends tokens
   to change the handler or add evidence artifacts, re-promotes at a new `tree_hash`, and
   resubmits for appraisal. Closing the failing clause is the concrete self-improvement loop.

## Testing & Validation Requirements
- Given a promoted app with valid RFC 0020 facts and a passing spec, the evaluator emits
  per-clause pass results with file/line evidence and the curated homepage lists the app.
- Given the same RFC 0020 facts under two curator specs, one accepts and one rejects; this proves
  curation is policy over facts, not a universal platform verdict.
- A failed clause appears in the verdict and as an RFC 0021 spend target; after a code/evidence
  change and re-promotion at a new `tree_hash`, the evaluator judges the new source.
- The raw attested list still includes attested apps that fail curation; the curated view hides
  or separates them without deleting their evidence bundle.
- Tampering a stored verdict's `source_tree_hash`, `spec_hash`, or `evaluator_hash` causes the
  repro check to fail.
- An attested-evaluator deployment serves its own RFC 0020 facts; a consumer can verify the
  evaluator before accepting its verdict as curator C's policy output.

## Report Requirements
- A sample spec with at least three clauses, including one broker/secrets clause and one
  source-inspection clause.
- A sample verdict JSON showing pass, fail, evidence pointers, and a remediation hint.
- A transcript showing the same RFC 0020 facts judged under two specs with different outcomes.
- A homepage/listing screenshot or HTML transcript showing curated badges and the full-verdict
  link.
- A self-improvement transcript: failed clause → RFC 0021 spend target → new promotion →
  re-judged passing clause.

## Out of Scope
- Changing RFC 0020 `verify()` to return accept/reject. It remains a facts API.
- Defining a universal platform safety score, trust grade, or canonical curator.
- Proving an LLM judge is sound. This RFC requires reproducible inputs, evaluator identity, and
  per-clause evidence pointers; stronger judge design is an implementation choice.
- Automatically fixing apps. RFC 0021 covers the spend loop; this RFC only supplies the target.
- Secret/credential broker implementation details beyond the clauses a spec may assert
  (RFC 0018).

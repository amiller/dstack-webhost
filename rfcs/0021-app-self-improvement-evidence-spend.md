# RFC 0021: App Self-Improvement via Attestation-Evidence Spend

## Summary
Let an attested app *spend tokens to strengthen its own attestation evidence*, where that
work doubles as app improvement. RFC 0020 defines the evidence bundle a consumer checks and
the `verify()` that returns facts; this RFC is the loop that *invests* in more and stronger
facts over time — generating consumer-facing reproduction stubs, shrinking the source an
auditor must read, and pinning the invariants the handler claims. Better evidence makes the
per-app audit smaller, which is the platform's whole payoff (`index.md`, "why audits stay
small"); the same work makes the app better.

## Problem
After promotion an app's evidence is static. `deploy.py` records the source hash at promote
time and `ingress.py` (RFC 0015/0020) serves it; nothing improves between promotions. When
the operator shares an app with someone and wants it to be *more* trustworthy, there is no
defined way to put marginal effort toward that:

- **Evidence is fixed at the hash, not cultivated.** The bundle carries the four layers
  (channel, platform, per-app binding, source) but no per-app artifacts that make a
  consumer's re-check cheaper — no reproduction stubs, no pinned-invariant tests, no
  narrowed-scope notes.
- **"Spend to strengthen" has no target.** An agent willing to spend tokens improving an app
  has nowhere to aim: the same token can harden the handler *or* enrich its evidence, and
  today only the former is even expressible.
- **The audit-shrinking loop is asserted, not driven.** `index.md` claims audits stay small
  because the substrate is learned once and the per-app handler is small. Nothing rewards an
  app for actually getting smaller / more legible at the per-app layer.

This is the **dstack-webhost app** flavor of "self-improving." There is a separate adapter
loop (an oauth3 browser adapter spending tokens to get reified into a replayable API call —
saved traces, reverse-engineered API search, generated test cases) that belongs in the
oauth3/teleport repo, not here. Keeping them apart is deliberate: this RFC's "spend" produces
*consumer-checkable evidence*; that one's produces *a cheaper execution path*.

## Files to Modify
- `verify/` (RFC 0020) — extend the bundle/facts with optional per-app evidence artifacts:
  reproduction stubs, pinned-invariant test results, a scope/legibility note. Facts only, no
  verdict — same posture as 0020.
- `proxy/deploy.py` — carry the per-app artifacts in the promotion record so they bind at the
  same `tree_hash` as the source they describe.
- `proxy/ingress.py` — serve the artifacts alongside the 0020 evidence bundle at the
  verification endpoint.
- `proxy/audit.py` — record evidence-spend actions (artifact added/refreshed) so the
  cultivation history is itself auditable.
- New `evidence/` convention in a project tree — where an app keeps its reproduction stubs,
  invariant tests, and scope notes, hashed into the binding like the rest of the source.

## Implementation
1. **Evidence artifacts, bound at `tree_hash`.** A promoted app may carry, under an
   `evidence/` dir: (a) **reproduction stubs** — the exact on-chain call / quote-field check a
   consumer runs to re-derive each 0020 layer against Base / Phala / Intel PCS directly; (b)
   **invariant tests** — cases that pin the property the handler claims, so "this holds" is a
   runnable fact not a sentence; (c) a **scope note** — what the handler does *not* touch,
   narrowing what an auditor must consider. All hashed into the binding so they cannot drift
   from the source they describe.
2. **`verify()` returns them as facts.** The bundle gains `evidence: { repro_stubs[],
   invariants: { passed, total, ... }, scope_note }`. The library renders no verdict; a
   consumer policy may *require* certain artifacts (e.g. "invariants all pass" or "repro stub
   for the on-chain layer present") — that policy lives in the consumer, per 0020.
3. **The spend loop.** An agent (the app's own maintenance pass, or the operator's) invests
   tokens to: write a missing repro stub, add an invariant test, shrink/rewrite the handler so
   the scope note is smaller and truer, then re-promote. Each pass strictly grows the
   consumer-checkable surface or shrinks the audit surface — never both directions at once, so
   "improvement" is measurable.
4. **Audit-shrink as the objective.** Surface a per-app *legibility* signal in the facts
   (e.g. handler LOC the auditor must read at this hash, count of pinned invariants). This is
   not a score the platform renders as good/bad — it is a fact a consumer or the operator can
   trend across promotions to see the app getting more legible.
5. **Opt-in, additive.** An app with no `evidence/` dir behaves exactly as RFC 0020 today; the
   bundle's `evidence` block is simply absent. Nothing forces existing apps to grow artifacts.

## Testing & Validation Requirements
- A promoted app with an `evidence/` dir serves repro stubs + invariant results in the bundle;
  `verify()` returns them as facts, still renders no verdict.
- Tamper an evidence artifact (edit a stub) without re-promoting → `tree_hash` mismatch
  surfaces in facts; the artifact cannot drift from its source silently.
- A consumer reference policy that *requires* "all invariants pass" accepts an app with green
  invariants and rejects one with a failing/absent invariant — from identical library facts.
- An evidence-spend pass (add one invariant test, re-promote) appears in `audit.py`'s log and
  increments the pinned-invariant count in the facts.
- An app with no `evidence/` dir verifies identically to its pre-RFC-0021 behavior.

## Report Requirements
- A sample bundle with the `evidence` block populated (repro stubs + invariant results +
  scope note).
- A transcript of two promotions of the same app showing the legibility signal moving in one
  direction (more invariants pinned, or smaller handler) across the pair.
- The require-an-invariant consumer policy accepting and rejecting from identical facts.

## Out of Scope
- The oauth3 adapter-reification loop (browser → replayable API call). Different repo, different
  kind of spend.
- Any platform-rendered quality score or "trust grade." Legibility is a fact a consumer trends;
  the accept/reject policy stays in the consumer (RFC 0020).
- Automated generation of the artifacts. This RFC defines where they live, how they bind, and
  how `verify()` exposes them; *who* writes them (a maintenance agent vs. the operator) is an
  orchestration concern, not a daemon contract.
- Secret/credential handling for the spend itself (RFC 0018).

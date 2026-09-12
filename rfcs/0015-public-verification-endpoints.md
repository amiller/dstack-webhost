# RFC 0015: Public Read-Only Verification Endpoints

## Summary
Open read-only verification endpoints to anonymous callers for projects in attested mode, so a relying party can verify what's running without holding the daemon's admin token.

## Problem
Every `/_api/*` request currently passes through `_check_auth` in `proxy/ingress.py` (line 391, called from `_handle_api` at line 403). That includes endpoints whose entire purpose is to let an external party verify what code is running:

- `GET /_api/projects/<name>` — project manifest (source, commit, tree hash)
- `GET /_api/projects/<name>/audit` — audit log
- `GET /_api/attest/<name>` — dstack attestation quote
- `GET /_api/verification/<name>` — combined manifest + quote + audit

Today all four return `401 missing token` to an anonymous caller. The `proxy/verify.py` CLI documents this by accepting `--token` — but a relying party does not have the daemon admin's token, and asking for one defeats the point of attestation.

This blocks the iconic relying-party demo on the project's front page and breaks the trust chain laid out in RFC 0001.

## Files to Modify
- `proxy/ingress.py` — `_handle_api()` (line 402) and a new helper for public-readable endpoints

## Implementation
1. In `_handle_api`, do not gate every path through `_check_auth`. Instead, classify each path before the auth check:
   - **Public, always-readable:** none yet.
   - **Public, attested-only:** `GET /_api/projects/<name>`, `GET /_api/projects/<name>/audit`, `GET /_api/attest/<name>`, `GET /_api/verification/<name>`. Resolve the project; if it exists and `mode == "attested"`, serve. If it does not exist or is in `dev` mode, return `404 not found` (do not leak existence of dev projects).
   - **Authenticated:** all mutating endpoints (POST, DELETE), plus list endpoints (`GET /_api/projects`, `GET /_api/routes`, `GET /_api/audit`) which would otherwise enumerate dev projects.
2. Public endpoints that take no project name (e.g., a future `GET /_api/instance` for the daemon's own attestation) should also be public. Do not add this in this RFC — keep scope to the per-project endpoints.
3. Document the public/private split in a docstring on `_handle_api` so the rule is reviewable in one place.
4. Do not change the `_check_auth` implementation itself. Only change which endpoints route through it.

## Testing & Validation Requirements
- With no `Authorization` header, `GET /_api/verification/<attested-name>` returns 200, and the body
  contains **only** the fields this RFC names as public evidence (identity + provenance:
  `name, runtime, mode, source, ref, commit_sha, tree_hash, deployed_at`). Assert on CONTENT, not
  just status: deploy a project carrying a sentinel value in `env` and assert the sentinel appears
  in no response body on any endpoint this RFC opens. `env` must never appear.
  *(Amended 2026-07-21 — see Revisions. The original bullet read "returns 200 and the full payload",
  which specified the leak as the acceptance criterion.)*
- With no `Authorization` header, `GET /_api/verification/<dev-mode-name>` returns 404 (not 401, to avoid leaking the existence of dev projects).
- With no `Authorization` header, `POST /_api/projects` still returns 401.
- With no `Authorization` header, `GET /_api/projects` (list) still returns 401, since enumerating includes dev projects.
- With a valid token, all endpoints behave as before.
- Promoting a project from dev to attested flips the public endpoint from 404 to 200; tearing it down flips back to 404.
- The existing `proxy/verify.py` CLI works against an attested project without `--token`.

## Report Requirements
- Diff of `_handle_api` showing the new classification.
- Curl transcripts for: anonymous read of an attested project (200), anonymous read of a dev project (404), anonymous mutation attempt (401), authenticated full access (200).
- Note: this RFC does not depend on the promote flow being fully working; it only changes which endpoints are publicly readable when a project is in attested mode. RFC 0001's promotion milestone is the separate prerequisite for those endpoints to actually return useful data.

## Out of Scope
- Rate limiting on the public endpoints (worth doing eventually; not required to unblock the verifier).
- Caching headers (same).
- A `/_api/instance` endpoint for the daemon's own attestation (separate RFC).

## Revisions

An RFC that opens a trust boundary is a live document: what is learned about that boundary afterwards
belongs here, in the thing the next implementer reads, not only in the commit that fixed it.

### 2026-07-21 — this RFC specified a secret leak, and did so for three months

**What happened.** Making these endpoints public was implemented as a *routing* change. The payloads
they already returned were not revisited. Those payloads were `asdict(project)`, which includes
`project.env`. From 2026-04-25 every attested project published its secrets to anyone who asked —
`BRIDGE_SECRET`, `OPENVPN_USER`/`PASS`, `OVPN_CONFIG_BASE64`, `ZAI_API_KEY`, OAuth client secrets,
neko passwords. Fixed 2026-07-21 in `156a82a6`; the payload is now a projection, not a serialization.

**Why this RFC is the right place to record it.** The same defect was correctly diagnosed on
2026-06-18 (`1bb89b7f`) on a sibling public route — root cause stated exactly, in a commit message.
That finding never reached this document. Eight days later `3c09e7e9` added the HTML renderer and
faithfully implemented what this RFC still asked for: *the full payload*. The spec outlived the
review, so the review did not count.

**The rule this yields.** An RFC that changes an endpoint's AUDIENCE owes a specification of what
crosses the boundary, not only who may cross it. Reachability tests verify that the door opens; they
are silent on what is behind it. On a boundary deliberately opened, content review is the only control
left — and it was the one nobody wrote down.

**Still open** (tracked separately, not fixed by `156a82a6`): `_api_status` guards the same
serialization behind a caller-supplied `public: bool` flag, so safety is opt-in; `_api_aggregate_status`
serializes `env` on an authenticated route. Both are the same pattern, and the sweep after 2026-06-18
is the sweep that should have caught this one.

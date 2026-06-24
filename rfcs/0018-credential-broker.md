# RFC 0018: Credential Broker — Scoped, Expiring, Auditable Delegations

## Summary
Replace static plaintext env secrets with a daemon-side broker that holds the upstream credential, hands each app a scoped and expiring delegation (or proxies the upstream call so the raw secret never enters the handler), logs every use, and supports revoke and reauthorize. This is the direct answer to accumulating "oauth3-style" apps that each carry standing key-exposure risk.

## Problem
Apps receive **full-scope secrets as plaintext env**. `proxy/runtimes.py` builds a project's environment from `project.env` and `env_passthrough`; those values live as plaintext in `projects/<name>/project.json` at rest, are injected wholesale into the handler, and once deployed there is:

- **No expiry.** Env secrets are static until the app is redeployed.
- **No revocation.** You cannot kill a leaked key without editing and redeploying.
- **No scopes.** The handler gets the whole credential, not a narrowed capability.
- **No usage log.** The per-project audit log (`proxy/audit.py`) records management actions (deploy/teardown/promote), not credential *use*.
- **No reauthorize/rotate flow.**

The only credential in the system with a lifecycle is the `tunnel` token (`proxy/tunnel.py`): TTL-bounded, explicitly revocable, narrowly scoped — exactly the shape the rest of the delegations lack. As the operator's habit shifts to many small key-delegating apps, each one is a standing secret on the CVM with no way to expire, rotate, or even see when it was last used.

(Note: the dstack-*derived* per-app key is already handled well — the private key stays in the daemon after the 0389869a fix. This RFC is about the *upstream* secrets apps hold, not the derived key.)

## Files to Modify
- New `proxy/broker.py` — sealed credential store + grant model + issue/proxy/revoke + usage log. `proxy/tunnel.py` is the structural model (TTL, revoke, JSON-backed store, recovery).
- `proxy/ingress.py` — handler-facing broker endpoint(s); admin grant/list/revoke/reauthorize endpoints; expose grants to the console (RFC 0016).
- `proxy/runtimes.py` — stop injecting raw secrets for broker-managed credentials; resolve a `broker:<grant-id>` env reference to a handle at request time instead.

## Implementation
1. **Grant model**, stored **sealed** under the dstack-derived key (so secrets are ciphertext at rest — this also fixes the plaintext-`project.json` exposure): `{id, project, name, scope, upstream, ttl/expires_at, created_at, revoked}`. The raw upstream secret is held by the broker, never written to a project manifest.
2. **Two delegation modes:**
   - **Token issue** — the handler calls the broker for a short-lived bearer it forwards to the upstream itself. Expiry enforced broker-side.
   - **Proxy mode (stronger)** — the handler calls a broker endpoint; the broker attaches the secret and forwards to the *pinned* upstream, returning the response. The raw secret never reaches the handler. This mirrors the tunnel relay and is the preferred mode for the "small delegation" apps.
3. **Usage log:** every issue/proxy call appends `{ts, project, grant_id, scope, upstream, outcome}` to `creds/<project>.jsonl`. This is the audit surface the operator wants — see every use, spot anomalies, decide what to expire. Keep it a separate file from the RTMR-extended management audit so high-volume use events don't bloat the measurement log.
4. **Lifecycle endpoints (authed):** `POST` grant, `DELETE` revoke (immediate — broker refuses the grant thereafter), `POST` reauthorize (rotate the underlying secret and/or extend TTL). `GET` list grants per project.
5. **Console integration (RFC 0016):** show grants per app — scope, last used, expires — with a revoke action.
6. **Opt-in migration, no big-bang:** existing apps keep their env. A project opts a value into the broker by setting it to `broker:<grant-id>`, which `runtimes.py` resolves at request time. Nothing forces a rewrite.

## Testing & Validation Requirements
- An issued token is refused by the broker after its TTL elapses.
- Revoke takes effect immediately — the next handler call fails closed.
- In proxy mode, the raw secret is **absent** from the handler's `ctx.env` (inspect it) yet the upstream call still succeeds.
- Every issue/proxy call appears in `creds/<project>.jsonl`.
- The sealed store is ciphertext at rest (the secret is not grep-able in `/var/lib/tee-daemon`).
- Reauthorize rotates the secret without redeploying the app.

## Report Requirements
- Transcript: grant → use → expire (refused) → revoke (refused immediately).
- Proof the secret is absent from the handler env in proxy mode.
- A sample `creds/<project>.jsonl` usage log.

## Out of Scope
- Full third-party OAuth provider flows (this brokers credentials the operator already holds; it is not an OAuth authorization server).
- Per-request human approval UI (a possible follow-up; the MVP is grant/expire/revoke + usage log).
- Automatic rotation against upstreams that have no rotation API.
- The derived-key model, which is already covered (0389869a).

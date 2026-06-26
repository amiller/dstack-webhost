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


## Design

### 0. Where the broker lives

A new `proxy/broker.py` holds the sealed grant store and the issue/proxy logic.
`proxy/tunnel.py` is the structural template: a dataclass record, a JSON-file-per-id
store under a base dir, in-memory index, TTL/`is_expired()`, immediate delete, and a
`recover()` that re-reads the dir on boot. The broker reuses that shape and adds
sealing on top.

Wiring in `proxy/main.py` (mirrors how `TunnelStore` and `DstackProxy` are set up):

- `DAEMON_BROKER_DIR = /var/lib/tee-daemon/broker`   — sealed grant files
- `DAEMON_CREDS_DIR  = /var/lib/tee-daemon/creds`     — per-project usage logs
- `BrokerStore(DAEMON_BROKER_DIR, dstack_sock)` is constructed after `dstack_sock`
  is resolved (line 40), `recover()`-ed on boot alongside `tunnel_store.recover()`,
  and passed into `Ingress`.
- The handler-facing endpoint is a **second unix socket served in
  `BROKER_SOCKET_DIR`**: `creds.sock`, served by an `aiohttp` `UnixSite` exactly like
  the filtered `dstack.sock` (main.py lines 78–91). Because it sits in
  `BROKER_SOCKET_DIR`, it rides the existing `BROKER_VOLUME_NAME` mount and appears
  inside apps at `/run/broker/creds.sock`, next to `/run/broker/dstack.sock`. No new
  mount plumbing is needed — `runtimes._attested_broker_binds()` already binds that
  volume.

The sealing key comes from dstack and is only available in the TEE. On a host with no
`dstack.sock` (`main.py` already sets `dstack_sock = None` and disables the dstack
proxy), `BrokerStore` construction succeeds but any seal/unseal **raises** — grant
creation returns a clear 503, consistent with the existing "dstack not available"
behavior in `_api_attest`. No plaintext-key dev fallback.

### 1. The sealed grant store

**Sealing key.** The daemon (in-TEE) calls dstack `GetKey` on a dedicated path under
the already-allowed `KEY_PATH_PREFIX` (`dstack_proxy.ALLOWED_METHODS` includes
`GetKey`, gated to `/tee-daemon/`):

```
POST GetKey {"path": "/tee-daemon/broker/seal"}  ->  derived k256 private key (32 bytes)
```

This is the same primitive `_api_attest` uses, except the broker keeps the raw 32-byte
key in-process instead of sanitizing it (`_sanitize_getkey` exists precisely because
that response is the private key; the broker is an in-TEE caller and is allowed to hold
it). The key is deterministic across reboots — dstack re-derives the same value from the
app key + path — which is what makes recovery work without storing the key anywhere.

Derive an AEAD key and seal with AES-256-GCM:

```
seal_key = HKDF-SHA256(ikm = getkey("/tee-daemon/broker/seal")[:32],
                       salt = b"",
                       info = b"tee-daemon/broker/seal/v1", L = 32)
nonce    = os.urandom(12)
ct       = AESGCM(seal_key).encrypt(nonce, secret_utf8, aad = grant_id)
```

`aad = grant_id` binds ciphertext to its record so a sealed blob cannot be replayed under
a different grant. This adds one dependency: `cryptography` (AESGCM) — one line in the
Dockerfile next to `pip install aiohttp`.

**Record schema** (`proxy/broker.py`, `@dataclass Grant`, one JSON file per id under
`DAEMON_BROKER_DIR`, mirroring `tunnels/<id>.json`):

```jsonc
{
  "id":          "g-AbC12xYz",          // "g-" + token_urlsafe(8)
  "project":     "my-app",
  "name":        "openai",              // operator label; what the handle points at
  "mode":        "proxy",               // "proxy" | "issue"
  "scope":       "chat.completions",    // free-form; logged + used as path/method gate
  "upstream": {                         // see §2 for the two shapes
    "base_url":      "https://api.openai.com",
    "allow_paths":   ["/v1/chat/completions"],
    "allow_methods": ["POST"],
    "inject":        {"header": "Authorization", "template": "Bearer {secret}"}
  },
  "sealed":      {"nonce": "…hex…", "ct": "…hex…"},   // the upstream secret, ciphertext
  "created_at":  "2026-06-26T…Z",
  "expires_at":  "2026-07-26T…Z",       // null = no expiry
  "last_used_at":"2026-06-26T…Z",       // updated on each issue/proxy call
  "revoked":     false,
  "require_approval": false             // reserved; see §5
}
```

The raw secret is **never** written to `project.json` or returned by any read endpoint —
only `sealed` is on disk, and it is ciphertext. This is the concrete fix for the
plaintext-`project.json` exposure: `project.env` now carries a handle (`broker:<id>`),
the secret lives only in `broker/<id>.json` as `sealed`.

**Recovery on boot.** `BrokerStore.recover()` re-reads every `*.json` in
`DAEMON_BROKER_DIR`, drops expired/revoked records (delete file, like
`TunnelStore.recover`), and rebuilds the in-memory index. Secrets stay sealed in memory
as `sealed` blobs and are unsealed lazily per request, so a heap dump of the daemon does
not trivially yield every secret at once.

**Test hook (matches RFC's validation list):** `grep -r <secret> /var/lib/tee-daemon`
returns nothing — only ciphertext is at rest.

### 2. The two delegation modes

Both modes are reached over `/run/broker/creds.sock`. They differ in whether the secret
ever touches the handler.

#### (a) Token-issue — `POST /token/<grant-id>`

The broker uses the sealed *parent* secret to mint a short-lived *child* credential via
the upstream's own token-exchange/STS endpoint, and returns the child token to the
handler, which forwards it upstream itself.

```
handler ── POST /token/g-… ──▶ broker
broker: unseal parent secret → call upstream.token_exchange → child {token, exp}
broker ── {token, expires_at} ──▶ handler ──▶ upstream (handler holds child token)
```

`expires_at` is bounded to `min(remaining grant TTL, upstream-max, grant.max_token_ttl)`
and the broker refuses to mint after the grant is revoked/expired — that is the
"expiry enforced broker-side" line: the broker controls *whether* a fresh token is
issued, even though the upstream validates the token after that.

**When to use:** only when the upstream actually exposes a minting/exchange API
(OAuth token-exchange, cloud STS `AssumeRole`, GitHub App installation tokens, etc.).
A child token is genuinely narrower and self-expiring, so for those upstreams this is
the stronger story. For a plain static API key with no minting API, token-issue is
**not possible** — use proxy mode. (See §5, "upstreams with no rotation API".)

#### (b) Proxy mode — the broker attaches the secret to a pinned upstream

The handler makes the upstream call *through* the broker; the secret is injected
broker-side and never reaches `ctx.env` or the handler. This mirrors the tunnel relay
(`_handle_tunnel`) and is the default for the "many small key-delegating apps" case.

**Request contract** (handler → broker socket):

```
<METHOD> /proxy/<grant-id><upstream-subpath>     on /run/broker/creds.sock
headers:  whatever the handler wants forwarded, MINUS the credential
          X-Broker-Token: <per-container caller token>   (see §3 identity)
body:     passthrough
```

The handler supplies only the **subpath** (e.g. `/v1/chat/completions`) — never a host.

**Broker steps:**
1. Load grant by id; reject if missing/`revoked`/expired (fail closed → 403/410).
2. Authenticate caller (`X-Broker-Token` → project; check `== grant.project`, §3).
3. Enforce the pin: method ∈ `upstream.allow_methods`; subpath prefix-matches one of
   `upstream.allow_paths`. The destination host is `upstream.base_url` from the sealed
   record — **the handler cannot influence it.** This is what "pinned upstream" means:
   host is fixed in the grant, path/method are allow-listed, so a compromised handler
   can at most replay the credential against the exact endpoints the operator scoped.
4. Unseal the secret and inject per `upstream.inject` (header `Authorization: Bearer …`,
   or a query param, or a fixed header name) into the outbound request.
5. Forward to `f"{base_url}{subpath}"`, stream the response back verbatim
   (reuse the `aiohttp` request/relay pattern already in `_proxy`/`_handle_tunnel`).
6. Append a usage record (§4).

**Response contract:** upstream status/headers/body, unmodified. The injected
credential header is request-only and never echoed.

**When to use:** the default. Always available (no upstream minting API required), and
the only mode where the raw secret provably never enters the handler — which is the
RFC's headline validation ("the raw secret is absent from the handler's `ctx.env`").

### 3. Env resolution change in `runtimes.py`

Today `runtimes.py` injects raw secrets three ways: the shared router reads
`manifest.env` directly (`envs.set/​envs[name] = manifest.env`), `start_isolated` folds
`project.env` into `json.dumps(env)` argv, and `start_image` emits `KEY=val` env. The
change is deliberately small and **backward-compatible**: a plain string stays a plain
string; only the literal handle form `broker:<grant-id>` gets special treatment.

1. **Handles pass through unchanged.** `env["OPENAI_KEY"] = "broker:g-AbC12xYz"` is just a
   string; it flows into `ctx.env` as-is. The handler (or a thin SDK helper) sees a
   handle, not a secret. For **proxy mode** the handler never needs to resolve it — it
   calls `/proxy/g-AbC12xYz/...`. For **token-issue** it calls `/token/g-AbC12xYz`.
   "Resolved at request time" = resolution happens when the handler calls the broker
   socket, not at container start. No raw secret is ever materialized into env.

2. **Mount the creds socket for any project that references a broker handle.** A new
   helper `_project_uses_broker(project)` returns true if any `env` value starts with
   `broker:`. Generalize `_attested_broker_binds(mode)` (currently gated on
   `mode == "attested"`) so the `creds.sock`-bearing volume is also bound when
   `_project_uses_broker(project)` — broker grants are not exclusive to attested apps.
   `dstack.sock` stays attested-only; `creds.sock` follows broker usage.

3. **Inject the socket path + caller token.** Add to the per-container env:
   `BROKER_SOCKET=/run/broker/creds.sock` and `BROKER_TOKEN=<bt-…>`. The daemon mints
   `BROKER_TOKEN` at container start, keeping a `{token → project}` map in
   `RuntimeManager` that `creds.sock` consults to authenticate the caller (step 2 of the
   proxy flow). For `start_isolated` (deno `--deny-env`, env via argv) the token rides
   the same `json.dumps(env)` channel; the `creds.sock` path must be added to
   `--allow-read=`/`--allow-write=` exactly as `broker_sock` already is for `dstack.sock`
   (lines 468–469), and `--allow-net` is already present.

4. **Caller identity granularity (honest limitation).** In `isolation:container` and
   `image` runtimes each project is its own container, so `BROKER_TOKEN → project` is a
   real per-project binding and `grant.project` is enforceable. In the **shared** deno
   runtime, many projects run in one `--allow-all` V8 isolate with one env (the known
   multi-tenancy gap); a single `BROKER_TOKEN` there authenticates "this shared isolate,"
   not an individual co-tenant. Co-tenants in the shared runtime are co-trust by
   construction (the `runtimes.py` comment at lines 384–390 already says this), so the
   `grant.project` check there is advisory. **Real upstream secrets should use
   `isolation:container`.** This is a documented constraint, not a fallback.

### 4. Lifecycle + API

**Admin endpoints** (on the existing ingress `_handle_api`, behind `_check_auth` /
`API_TOKEN`, same as the tunnel endpoints):

| Method & path                              | Effect |
|--------------------------------------------|--------|
| `POST   /_api/grants`                       | Create. Body `{project,name,mode,scope,upstream,secret,ttl}`. Secret is sealed and consumed; response is `{id,expires_at}` — **secret never returned.** |
| `GET    /_api/grants?project=<p>`           | List grants for a project: `name, mode, scope, upstream.base_url, created_at, expires_at, last_used_at, revoked`. No secret, no `sealed`. |
| `DELETE /_api/grants/<id>`                   | **Revoke — immediate.** Sets `revoked=true`, deletes the sealed file; next `issue`/`proxy` fails closed. Mirrors `_api_delete_tunnel`. |
| `POST   /_api/grants/<id>/reauthorize`      | Body `{secret?, ttl?, scope?}`. Rotate the sealed secret and/or extend TTL and/or re-scope, **same id** — the app keeps its `broker:<id>` handle and is not redeployed. |
| `GET    /_api/grants/<id>/usage`            | Tail of this grant's usage from `creds/<project>.jsonl`. |

**Usage log** — `creds/<project>.jsonl`, append-only, written by `proxy/broker.py`
(not `audit.py`). One line per `issue`/`proxy` call:

```jsonc
{"ts":1750000000.12,"project":"my-app","grant_id":"g-AbC12xYz","mode":"proxy",
 "scope":"chat.completions","upstream":"https://api.openai.com","method":"POST",
 "subpath":"/v1/chat/completions","outcome":"200"}
// outcome ∈ upstream status | "denied-path" | "denied-method" | "expired" | "revoked"
```

This is intentionally **separate from the RTMR-extended management audit**. `audit.py`'s
`AuditLog.record()` calls `_extend_rtmr` (an `EmitEvent` per entry) — fine for a handful
of deploy/teardown/promote events, ruinous for high-volume credential *use*. So:

- **Per-use events → `creds/<project>.jsonl`** (plain append, no RTMR). High volume,
  operational visibility ("what used what, when, did it succeed").
- **Grant lifecycle events → `audit.py`** (RTMR-extended, attestable). Add actions
  `grant`, `revoke`, `reauthorize` to `AuditEntry.action`; these change the security
  posture and belong in the measured log a verifier can inspect via the existing
  `/_api/projects/<name>/audit`.

### 5. The hard parts

- **Per-request human approval — deferred, field reserved.** `Grant.require_approval`
  exists from day one. MVP behavior: a grant with `require_approval=true` is **denied**
  at the broker (fail closed) rather than silently approved — no fake "approval." The
  real flow (broker parks the call, returns `202 pending`, surfaces a prompt in the
  RFC 0016 console / a notification, releases on approve) is a follow-up that lands once
  the console has a write path. Designing the field now means the proxy/issue code path
  has the branch point and doesn't need reworking later.

- **Upstreams with no rotation API.** `reauthorize` does not require the upstream to
  support rotation. The operator rotates the key *on the upstream's side* (or pastes a
  newly-minted one), then `POST /_api/grants/<id>/reauthorize {secret: "<new>"}` swaps
  the sealed value under the same id. The app, still holding `broker:<id>`, picks up the
  new secret on its next request — **zero redeploy.** That is the entire value of
  reauthorize even with a dumb static key. *Automatic* rotation (broker calls an
  upstream rotation API on a timer) is the deferred enhancement and is the only part that
  needs a per-upstream adapter.

- **oauth3 runtime step-up rides reauthorize.** oauth3's runtime step-up (oauth3 spec
  series RFC 0005 — distinct from this repo's RFC 0005) needs to transiently widen a
  delegation's scope/TTL and later narrow it. It maps directly onto two existing
  primitives, no new broker API: step-up = `reauthorize(id, {scope: <wider>, ttl:
  <short>})`, step-down = a second `reauthorize` back to the narrow scope or a `DELETE`.
  Because reauthorize keeps the same id and the app holds a stable `broker:<id>` handle,
  the elevated capability takes effect at the app's next broker call without redeploy,
  and `require_approval` is the natural gate for a step-up that needs a human. The broker
  is the mechanism; oauth3 is the policy layer that calls it. This RFC does not specify
  oauth3 internals — only that the integration surface is `reauthorize` +
  `require_approval`.

### 6. Phasing

**MVP (lands on staging):**
- `proxy/broker.py`: `Grant` dataclass + `BrokerStore` (AES-256-GCM seal under the
  dstack-derived key, JSON-file-per-id, `recover()` on boot — modeled on `TunnelStore`).
- **Proxy mode** over `/run/broker/creds.sock`: pinned `base_url` + `allow_paths` /
  `allow_methods` + header injection. (This is the mode that proves "secret absent from
  `ctx.env`".)
- Lifecycle: `POST /_api/grants`, `GET` list, `DELETE` revoke (immediate),
  `POST /reauthorize` (operator-supplied secret / TTL).
- Usage log `creds/<project>.jsonl`; grant/revoke/reauthorize into the RTMR audit.
- `runtimes.py`: `broker:<id>` handles pass through; mount `creds.sock` when
  `_project_uses_broker`; inject `BROKER_SOCKET`/`BROKER_TOKEN`; per-container caller
  identity for `isolation:container` / `image`.
- Real secrets scoped to `isolation:container`; shared-runtime documented as co-tenant
  trust.
- One Dockerfile line: add `cryptography`.

**Deferred:**
- **Token-issue mode** (per-upstream token-exchange adapters; only useful where the
  upstream has a minting API).
- **Per-request human approval** UI (RFC 0016 console write path).
- **Automatic upstream rotation** (per-upstream rotation adapters).
- **Console grant view** (RFC 0016): scope / last-used / expires + revoke button.
- **oauth3 step-up policy layer** (consumes `reauthorize` + `require_approval`).

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

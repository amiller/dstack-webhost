# RFC 0033: OAuth3 as the Pod's Browser Login

## Summary
Give the daemon a **browser-facing authentication path** so a human owner can see and manage
all their apps — dev **and** attested — from a browser. Today the daemon's only credential is
the admin Bearer token (`TEE_DAEMON_TOKEN`, `ingress.py:51`), presentable only by `curl`/agents.
Every browser request is therefore anonymous, so the public homepage (`templates/index.html`)
and the RFC 0016 fleet console show only the anonymous *attested-or-public* view. A `dev` app
(e.g. Teleport) is invisible to its own owner in a browser.

This RFC makes the daemon an OAuth3 **relying party** whose identity provider is a pod app —
the `oauth3` project itself, already one of the 16 live projects (RFC 0023). Login reuses the
existing connect/approve handshake that `webhost-apps/feedling-web/oauth3-client.ts` already
drives against the pod. A localStorage token box is the **fallback only**, for a pod brought up
without OAuth3.

The shape is deliberately recursive and correct: **the pod hosts the app that logs you into the
pod.**

> **Correction (2026-08-19):** an earlier draft of this RFC analysed `oauth3-enclave`. The
> `oauth3` app deployed on this pod is **`oauth3-server`**, which exposes an HTTP whoami
> (`GET /api/me`) and opaque server-side sessions. See *The OAuth3 identity model* below — the
> single-owner compromise this RFC was built around is **not** required.

## Problem
Two owner-visible failures, one root cause.

- **The homepage hides dev apps even from the owner.** `templates/index.html:62` fetches the
  project list with `fetch('/', { headers: { Accept: 'application/json' } })` — **no
  Authorization header, ever**. That request lands on the anonymous branch, `ingress.py:116-117`:
  ```py
  visible = [p for p in self.store.list()
             if authed or p.mode == "attested" or p.public]
  ```
  With no token, `authed` is false, so only `mode == "attested"` or `public == true` projects are
  returned. A `dev` app that is not `public` is filtered out. The "~3 apps, most not attested,
  where's Teleport" symptom is exactly this filter working as coded against an anonymous caller.

- **The fleet console (RFC 0016) exists but is curl-only.** `/_api/status` and `/_api/console`
  sit behind the Bearer gate (`_check_auth`, `ingress.py:476-490`; dispatch gate at
  `ingress.py:572-575`). But `templates/console.html:282` fetches `/_api/status` with no
  Authorization header → 401, and the console HTML page is itself served from behind the gate, so
  a plain browser `GET /_api/console` 401s before the page can even load. The surface that groups
  **dev + attested** apps for the owner is real, and unreachable from a browser.

- **Root cause: there is no browser auth mechanism.** The owner-sees-everything branch
  (`authed`, `ingress.py:116`) and the scoped-token store (`TokenStore` / `ApiToken`,
  `proxy/tokens.py:32,54`) both already exist. Nothing in a browser ever *produces* a credential
  to trip them. The daemon has an admin token and a scoped-token API, but no login.

## Relationship to Other RFCs
- **RFC 0016 (Fleet Console)** built the *page* — the dev/attested grouped view. This RFC supplies
  the *credential* that makes it reachable. They are otherwise independent; 0033 does not change
  0016's page, only what a browser is allowed to see through it.
- **RFC 0018 (Credential Broker)** is the same "scoped token / owner-approved delegation" family;
  an OAuth3 login token is that pattern applied to the *management* surface rather than to app
  secrets.
- **RFC 0027 (Per-App Attestation)** is orthogonal: it governs the *attestation kind* of an app,
  not who may list it. This RFC governs listing/console visibility. "Attested" and "visible to the
  owner" must not be conflated — a dev app has no attestation and should still be fully visible to
  its owner once logged in.

## The OAuth3 Handshake We Already Have
`feedling-web/oauth3-client.ts` (`stepConnect`, lines 75-104) drives the pod's OAuth3 app:

1. `POST {node}/api/connect` `{ app, plugin }` → `{ requestId, approveUrl }`.
2. Owner opens `approveUrl` — the enclave serves a browser approval page
   (`oauth3/deployment-notes.md:70`, `GET /approve/:id`) — and **approves the request in their
   signed-in pod room**.
3. Poll `GET {node}/api/connect/{requestId}` → `{ status: "approved"|"denied"|"pending", token }`.
   The returned `token` is **scoped** to the requesting app.

This mints an **app→resource** capability (feedling → the youtube plugin). The console needs the
adjacent case: an **owner→pod-management** capability. Same handshake, different requested scope,
and the resulting token authenticates the *console session* instead of an app's plugin calls.

### The OAuth3 identity model (corrected — the pod runs `oauth3-server`, not `oauth3-enclave`)
**This section previously analysed the wrong codebase.** It described `oauth3-enclave`
(`proxy/src/auth.ts`, `server.ts`) — HS256 JWTs, no whoami, owner-role-gated `/approve/:id`.
The `oauth3` project deployed on this pod is **`oauth3-server`**
(`teleport/oauth3/oauth3-server`, a Deno webhost app), which has a different and *more capable*
identity model. Verified against the running prod pod:

```
$ curl -s https://pod.dstack.soc1024.com/oauth3/api/me
{"signedIn":false,"providers":{"github":true,"google":true,"openkey":true},"links":[]}
```

- **Sessions are opaque server-side tokens, not JWTs.** `createSession(subject)`
  (`server/sessions.ts:22`) mints `sess-<uuid><uuid>` and stores `{token, subject, createdAt}` in
  `sessions.json`; `verifySession` (`sessions.ts:29`) is a table lookup. Nothing is self-describing,
  so **no secret has to be shared** for a third party to validate one.
- **There IS an HTTP whoami: `GET /api/me`** (`server/handler.ts:264-265`) returns
  `{signedIn, subject, providers, links}` for whatever session is presented as a Bearer. The daemon
  *can* take a session token and ask "who is this" over HTTP. The previous claim that it could not
  was inherited from the enclave analysis.
- **`subject` is the identity to bind to.** `subjectOf()` (`handler.ts:161`) is
  `session?.subject ?? (isOwner(req) ? "owner" : null)`. A did:key sign-in yields
  `did:key:z6Mk…`; a federated sign-in yields a `u-…` subject; the owner-secret door yields
  `"owner"`. So `OAUTH3_OWNER` can be matched **exactly**, per-owner.
- **Sign-in is not listing-gated.** `STATIC_LISTING` (`server/listing.ts`) gates `POST /api/connect`
  only. `/api/login*` and `/api/me` are open, so the daemon does not need a curation entry to use
  this as a login provider.
- **The `JWT_SECRET` dev-open footgun does not apply.** That was an enclave property. The
  oauth3-server equivalent is fail-closed: `isOwner` (`handler.ts:131`) requires
  `!!ownerSecret`, so an unset `OAUTH3_OWNER_SECRET` disables the owner door rather than opening it.
- **Sessions do not expire.** `Session` carries `createdAt` but no expiry, and `verifySession` does
  not check age. The daemon must impose its own session TTL rather than trusting the oauth3 app's.

**Consequence for this RFC: the design below is more complicated than it now needs to be.** Both
shapes were built around "we cannot introspect a token, so a completed owner-gated *approval* must
stand in for identity." That constraint is gone. The simpler shape is:

> The browser already holds a pod-wide `oauth3_session` (shared `localStorage`, one origin — see
> the oauth3-sdk auth layer). It presents that session to the daemon; the daemon calls
> `GET {OAUTH3_NODE}/api/me` with it, reads `subject`, and issues its own session cookie iff
> `subject == OAUTH3_OWNER`. No connect/approve handshake, no shared secret, no polling, and
> real per-owner binding.

Shape (B)'s connect/approve flow remains valid as a fallback for a pod whose oauth3 app predates
`/api/me`, but it should no longer be the recommended default.

## Design

### Trust anchor: the daemon pins one OAuth3 owner
The daemon gains two config values (env, alongside `TEE_DAEMON_TOKEN`):

- `OAUTH3_NODE` — the in-pod URL of the `oauth3` app (reached daemon-side, not through the public
  gate). If **unset**, OAuth3 login is disabled and only the token-box fallback is offered.
- `OAUTH3_OWNER` — the OAuth3 principal / pod-room identity that maps to **owner scope**. This is
  the bootstrap anchor: the daemon trusts exactly this named identity as the owner. It is set by
  config, never self-asserted by a caller.

### Bootstrap edge (must not be circular)
The `oauth3` app must be **anonymously reachable** so login can run *before* you are logged in —
i.e. it is an `attested` or `public` project, served through the normal anonymous path. The
daemon→oauth3 calls are **in-pod** (localhost / broker socket), never gated by the console auth
they exist to establish. Owner identity is pinned by `OAUTH3_OWNER` config, so a compromised or
anonymous caller cannot claim to be the owner.

### Two candidate shapes (recommend B)
Because there is no whoami endpoint, neither shape can "POST a token, ask who it is." Identity comes
from the **owner-gated approval** itself (a completed permit ⇒ an owner approved), optionally
sharpened by a shared-secret local JWT verify.

**(A) Browser-driven, daemon shares `JWT_SECRET`.** The browser runs the `stepConnect` handshake
against `OAUTH3_NODE` (as feedling does), obtains the OAuth3 JWT, and presents it to the daemon.
`_check_auth` verifies the JWT **locally** with the enclave's `JWT_SECRET` (the exact HS256 check
of `auth.ts:34-44`) and grants owner scope iff `role == 'owner'` (and, if bound, `tenant_id ==
OAUTH3_OWNER`). *Cost:* the OAuth3 JWT lives in browser JS **and** the daemon must hold the
enclave's `JWT_SECRET` — which makes the daemon a **co-issuer** that can mint tokens for any tenant
(`signJWT`, `auth.ts:26-32`). That couples two apps' trust domains and is the wrong default.

**(B) Daemon-brokered login → session cookie (recommended, no secret sharing).** The daemon is the
OAuth3 client. It drives the connect/permit handshake server-side (as `teleport-pod/server.ts:82`
does: `POST {OAUTH3}/api/connect` → poll `/api/connect/:id`), presents the `approveUrl` to the
human, and polls until `status: approved`. Since `/approve/:id` is owner-role-gated
(`server.ts:244`), reaching `approved` **is** proof an owner approved in their signed-in oauth3
dashboard — the daemon needs no token introspection and never holds `JWT_SECRET`. It then issues
the browser an **HttpOnly session cookie** backed by a `TokenStore` `ApiToken` with owner scope
(`tokens.py:63`, unchanged). Owner *binding* (which owner) is handled per the poll response — see
step 3. Recommended: no token in JS, no cross-app secret, one revocable session primitive.

### Implementation (shape B)

1. **Un-gate the login shell.** Serve the console HTML like the homepage already is
   (`ingress.py:108-112`, unauthenticated) — the page is an empty shell; it must load so the
   login can run. Move the `GET /_api/console` HTML response ahead of the `_check_auth` gate (or
   serve the console at a browser path that mirrors the `index.html` branch). `/_api/status` stays
   gated.

2. **`GET /_api/login/start`** (unauthenticated): daemon does
   `POST {OAUTH3_NODE}/api/connect { app: "tee-daemon-console", scope: "pod:manage" }`, returns
   `{ requestId, approveUrl }`. The console renders "Approve this console in your pod room →
   `approveUrl`".

3. **`GET /_api/login/poll/{requestId}`** (unauthenticated): daemon polls
   `GET {OAUTH3_NODE}/api/connect/{requestId}`. On a terminal `approved`/`completed` the approval is
   already owner-proven. Owner **binding** — **CORRECTED, see the identity-model section above.**
   This step previously concluded that single-owner mode was forced, because the enclave's poll
   response carries no `owner_id`. On `oauth3-server` the daemon does not need the poll response to
   carry an owner at all: it resolves identity directly with `GET {OAUTH3_NODE}/api/me`
   (`handler.ts:264`) and compares the returned `subject` to `OAUTH3_OWNER`. **Per-owner binding is
   available now** and requires no change to the oauth3 app.
   - On success → `TokenStore.create(scope="/", ttl=SESSION_TTL)`, set it as an `HttpOnly; Secure;
     SameSite=Strict` cookie, return `{ status: "approved" }`. `pending`/`denied` → pass through.
   - The daemon sets its **own** TTL here, because oauth3-server sessions never expire
     (`sessions.ts` has no expiry check).

4. **`_check_auth` gains a session path** (`ingress.py:476-490`). Order after the `API_TOKEN`
   admin check: if the request carries a valid session cookie whose backing `ApiToken` is
   unexpired and owner-scoped, treat as authenticated (same effect as `authed == true`). Keep
   `TEE_DAEMON_TOKEN` as the break-glass admin credential and the scoped-token API unchanged.

5. **Make the homepage honor the session.** `templates/index.html:62` and `templates/console.html`
   send credentials with their fetches (`credentials: "same-origin"` so the cookie rides; if a
   token box is used instead, attach `Authorization: Bearer`). With a valid session the homepage
   `authed` branch (`ingress.py:116`) returns dev apps and the console `/_api/status` returns the
   full fleet → Teleport and every dev app appear.

6. **`POST /_api/logout`**: revoke the session `ApiToken` (`TokenStore.revoke`) and clear the
   cookie.

7. **Fallback token box (no-OAuth3 pods).** When `OAUTH3_NODE` is unset, `index.html`/`console.html`
   render a token field → store in `localStorage` → attach as `Authorization: Bearer` on fetches.
   This is the *only* browser auth for a pod without OAuth3, and is never shown when OAuth3 login is
   configured.

## Files to Modify
- `proxy/ingress.py` — un-gate the console HTML shell; add `/_api/login/start`,
  `/_api/login/poll/{id}`, `/_api/logout`; add the session-cookie path to `_check_auth`
  (476-490); the daemon-side OAuth3 client calls to `OAUTH3_NODE`.
- `proxy/tokens.py` — reuse `TokenStore` for session tokens; add a `session`/`owner` marker on
  `ApiToken` if session tokens must be distinguishable from API-issued scoped tokens (e.g. for
  listing/expiry policy).
- `proxy/templates/index.html` — send credentials on the list fetch; render a login control (OAuth3
  approve-link flow, or the token-box fallback) and a logged-in/owner indicator.
- `proxy/templates/console.html` — same credential handling on `/_api/status`; gate the page body on
  a logged-in session, show the login flow otherwise.
- Config/docs — document `OAUTH3_NODE` and `OAUTH3_OWNER`; note the fallback behavior when unset.

## Phasing
**Phase 1 — unblock the view immediately (no OAuth3 dependency).** Un-gate the console shell; make
`index.html`/`console.html` attach a credential; ship the **token-box fallback** and the
`_check_auth` session path keyed off a `TokenStore` token. This alone makes dev apps (Teleport)
visible to an owner who pastes the token — it works on any pod today, with no oauth3 app required.

**Phase 2 — OAuth3 becomes the login.** Add `OAUTH3_NODE`/`OAUTH3_OWNER`, the daemon-brokered
`/_api/login/*` handshake, owner-principal verification, and the session cookie. The token box
demotes to the no-OAuth3 fallback. This is the login Andrew actually wants: log into the pod via
the pod's own OAuth3 app, and the console recognizes you.

## Testing & Validation Requirements
- **Anonymous is unchanged.** With no session, `GET /` (JSON) and `/_api/status` behave exactly as
  today: homepage returns only attested/public; `/_api/status` 401s. No dev app leaks.
- **Owner sees dev apps.** After a successful login (OAuth3 or token box), the homepage list
  includes `dev` (non-public) projects and Teleport specifically; `/_api/status` returns the full
  fleet grouped dev/attested.
- **Only an owner-role approval mints a session.** A permit that never reaches a terminal
  owner-approved state mints no session; a forged/replayed `requestId` does not authenticate.
  (Per-owner `OAUTH3_OWNER` binding is only testable once the oauth3 app surfaces `owner_id`; until
  then single-owner mode is asserted.)
- **Bootstrap holds.** Login works when the caller has no prior credential (the console shell and
  `/_api/login/*` are reachable anonymously); the `oauth3` app is reachable anonymously; the
  daemon→oauth3 calls do not traverse the console gate.
- **Dev-open oauth3 is refused.** Pointed at an enclave running without `JWT_SECRET` (dev-open,
  `auth.ts:69-71`), the daemon does not grant an owner session — an unconfigured provider is not a
  valid login provider.
- **Session lifecycle.** `logout` revokes the session token; a revoked/expired session falls back to
  anonymous on the next request. `TEE_DAEMON_TOKEN` still authenticates via curl unchanged.
- **Fallback isolation.** With `OAUTH3_NODE` unset, only the token box is offered; with it set, the
  token box is not shown and the OAuth3 flow is the login.

## Report Requirements
- A browser transcript/screenshot of the homepage **before** (anonymous, attested-only) and
  **after** login (dev apps + Teleport visible).
- The console rendered from a logged-in `/_api/status` showing the dev section populated.
- A transcript of the OAuth3 login: `login/start` → approve in pod room → `login/poll` approved →
  session cookie set → owner view.
- A negative transcript: a non-owner principal approves → 403, no session.

## Open Questions & Hard Trade-offs
- **Token introspection / `whoami` — RESOLVED, and the earlier resolution was wrong.** The
  previous text concluded "no endpoint; not needed", from `oauth3-enclave`. The pod runs
  `oauth3-server`, which **does** expose whoami: `GET /api/me` (`server/handler.ts:264-265`)
  returns `{signedIn, subject, providers, links}` for any session presented as a Bearer, and
  sessions are opaque server-side records (`sessions.ts:22-31`), so validating one needs **no
  shared secret**. Shape (A)'s objection — that local JWT verification would make the daemon a
  token co-issuer — does not apply either. Single-owner mode is **not** forced; bind
  `OAUTH3_OWNER` to the returned `subject`.
- **~~The oauth3 app must be configured with a real `JWT_SECRET`.~~ — does not apply.** That
  dev-open footgun (`auth.ts:69-71`, every anonymous caller becomes `owner`) is an
  `oauth3-enclave` property. `oauth3-server` is fail-closed: `isOwner` (`handler.ts:131`) requires
  `!!ownerSecret`, so an unset `OAUTH3_OWNER_SECRET` disables the owner door instead of opening it.
  The real deployment invariant to keep is the daemon imposing its own session TTL, since
  oauth3-server sessions do not expire.
- **Scope granularity.** Owner-only (one identity, full management) vs. OAuth3-scoped operators
  (e.g. a read-only console viewer, a deploy-only operator) mapped to `TokenStore` scopes. Start
  owner-only; the scope machinery already exists (`scope_allows`, `tokens.py`) to extend later.
- **Session vs. bearer for the browser.** HttpOnly cookie (recommended: no token in JS, CSRF via
  `SameSite=Strict`) vs. a token in `localStorage` (simpler, but the credential is script-readable).
  The token box fallback is necessarily the latter.
- **Multi-owner / delegation.** A single `OAUTH3_OWNER` is the v0. Multiple owners, or delegating a
  scoped console session to a teammate, is a later extension (naturally OAuth3-shaped — it is a
  delegation-restriction system per its own design).
- **Cross-origin.** If the console and the oauth3 app are served on different hostnames/ports,
  shape (A)'s browser-side handshake needs CORS from the oauth3 app; shape (B) avoids this entirely
  by keeping the handshake daemon-side.

## Out of Scope
- Changing what `verify()` returns or the attestation model (RFC 0020 / 0027).
- The curated/appraised listing (RFC 0022) — this RFC governs *authenticated visibility*, not
  curation of the public list.
- OAuth3's internal enclave/room/approval implementation — this RFC consumes its connect/approve
  contract, it does not define it.
- Closing the shared-isolate multi-tenancy gap or any change to app runtime trust.

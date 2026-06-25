# tee-daemon / dstack-webhost -- Known Issues & Improvement Notes

Collected during end-to-end testing on hermes-staging CVM (2026-04-05).

---

## INGRESS

### 1. No client IP forwarding
**Severity:** Medium
**Where:** `proxy/ingress.py` `_proxy()` (line ~98)
**Problem:** The ingress does not add `X-Forwarded-For` or `X-Real-IP` when proxying to runtimes. The deno runtime sees `host: 172.19.0.3:3000` (internal docker IP) and has zero client IP info.
**Fix:** Add `headers["X-Forwarded-For"] = request.remote` (or `request.peername`) in `_proxy()` before forwarding. Also consider `X-Forwarded-Proto`.

### 2. No streaming / WebSocket support
**Severity:** High (blocks real-time use cases)
**Where:** `proxy/ingress.py` `_proxy()` (lines 100-111)
**Problem:** `_proxy()` does `await request.read()` then `await resp.read()` and returns a single `web.Response`. This means:
  - No 101 upgrade handling (WebSocket impossible)
  - No chunked/streaming response (SSE, large downloads)
  - Entire request/response buffered in memory
**Current workaround:** Long-polling with 25s timeouts in the tunnel app. Adds ~500ms latency per request.
**Fix:** Rewrite `_proxy()` to use `aiohttp.web.StreamResponse` with bidirectional streaming. Handle `Connection: Upgrade` / 101 responses. This is the single biggest limitation.

### 3. No request logging / observability
**Severity:** Low
**Problem:** Ingress logs nothing about proxied requests. No access log, no timing, no status codes. Makes debugging very hard.
**Fix:** Add structured logging in `handle()` with method, path, status, duration.

---

## DEPLOY

### 4. Deno module caching -- runtime doesn't pick up new code
**Severity:** High (causes confusing "old code runs" bugs)
**Where:** `proxy/runtimes.py` + the Deno router
**Problem:** When a new project is deployed via multipart upload, the files are written to the shared volume but the running deno container's `import()` caches the old module. `rtm.refresh()` recreates the container, but only when the LAST project using that runtime is torn down. Adding a new project to an already-running container does nothing visible -- the router's initial module scan already ran.
**Current workaround:** Must tear down ALL projects on that runtime, wait for container to die, then redeploy everything. Very painful for iteration.
**Fix options:**
  - (a) Router watches the projects dir with `Deno.watchFs()` and hot-reloads
  - (b) Expose a `/__reload` endpoint on the router that re-imports modules
  - (c) Deploy endpoint sends a signal to the runtime container after writing files
  - (d) Always restart the runtime container on any deploy (simplest but slowest)

### 5. multipart/form-data deploy API inconsistency
**Severity:** Medium
**Where:** `proxy/deploy.py` (running version vs. repo version)
**Problem:** The running daemon on the CVM expects `multipart/form-data` with a `manifest` field (JSON string) and file fields. The newer code in the repo expects a JSON body for the manifest. This mismatch makes it impossible to deploy to the running daemon using the repo's own client code.
**Fix:** Pick one format and stick with it. Multipart is more flexible (can include files inline). Recommend keeping multipart as the canonical format and updating the repo code to match.

### 6. No git in the deno runtime container
**Severity:** Medium
**Where:** Deno Dockerfile
**Problem:** Git-clone-based deploys silently fail because the deno container doesn't have `git` installed. The deploy succeeds (files dir created) but the files are empty.
**Fix:** Add `git` to the deno Dockerfile, or detect and error clearly when git is needed but missing.

### 7. Teardown doesn't always restart the runtime container
**Severity:** Medium
**Where:** `proxy/runtimes.py` `refresh()`
**Problem:** If project A and B share a runtime, tearing down A leaves the container running for B. Redeploying A writes new files but the container never restarts, so old cached code keeps running.
**Fix:** `refresh()` should always restart if there are ANY remaining projects (to pick up changes to the just-redeployed one), or at minimum warn that a restart is needed.

---

## TUNNEL APP

### 8. Tunnel relay is slow (long-poll adds ~500ms+ latency)
**Severity:** Medium (acceptable for demo, not for production)
**Where:** `examples/tunnel/server.ts` + `examples/tunnel/client.ts`
**Problem:** Every visitor request goes through: visitor -> ingress -> tunnel queues request -> client polls (500ms interval) -> client fetches localhost -> client POSTs relay -> tunnel resolves promise -> response goes back through ingress. Best case 500ms+ per request.
**Fix:** Blocked by issue #2 (WebSocket/streaming). Once ingress supports streaming, switch to WebSocket relay for sub-100ms latency.

### 9. Tunnel secrets in URL paths
**Severity:** Low (64-char hex is unguessable, but still)
**Problem:** The tunnel ID (authentication token) is in the URL path. This means it appears in browser history, server logs, referrer headers.
**Fix:** Consider moving to a header-based auth model for the poll/relay endpoints at least.

---

## GENERAL / PLATFORM

### 10. No health checks on runtime containers
**Severity:** Medium
**Problem:** After deploying, the ingress just assumes the runtime is up. There's no readiness probe or health check. If the deno container crashes on startup (e.g., bad module import), the ingress happily 502s with no diagnostic.
**Fix:** Add a health check endpoint (`/__health`) to each runtime container. Poll it after deploy before reporting success.

### 11. No rolling updates / zero-downtime deploys
**Severity:** Low (currently)
**Problem:** Deploying requires killing the entire runtime container. All projects on that runtime go down simultaneously.
**Fix:** Future -- run multiple runtime instances, do blue-green or canary deploys.

### 12. Volume mount path assumption
**Severity:** Low
**Problem:** Deno handler uses `import.meta.url` to find sibling files, but the exact path depends on how the router mounts and imports modules. The router imports from `/daemon-vol/projects/<name>/files/server.ts` so `import.meta.url` resolves to that directory. This works but is fragile.
**Fix:** Pass the project's files directory as an env var (`__PROJECT_DIR`) so handlers don't need to infer it.

---

## SECRETS / ISOLATION

### 13. `env_passthrough` not honored for `isolation:container` (deno/bun) -- blocks non-committed secrets for attested source-handlers
**Status:** RESOLVED (2026-06-24) -- `start_isolated()` now folds `env_passthrough` (resolved from the daemon's `os.environ`) into the deno argv env, mirroring `start_image()`. So an isolated, source-hash-attested handler can receive `SEAL_KEY`/`OWNER_SECRET` from the daemon's dstack-encrypted env without committing them. (Shared deno runtime still uses `manifest.env` only -- separate, lower-priority gap.)
**Severity:** Medium
**Where:** `proxy/runtimes.py` `start_isolated()` (lines ~441-455) vs `start_image()` (lines ~502-506)
**Problem:** `start_image()` resolves `project.env_passthrough` from the daemon's own `os.environ` and injects those vars into the container -- the intended channel for deploy-time secrets that must NOT be baked into (attested, world-readable) project source. `start_isolated()` (the deno/bun `isolation:container` path) ignores `env_passthrough`: it passes only `project.env` (from the committed `project.json`) through the argv shim, and `--deny-env` leaves the handler no other way in. Net: an isolated, source-hash-attested handler cannot receive a secret at all. Concrete case: the teleport-plugins `otter` handler needs a `SEAL_KEY` to AES-GCM-seal a synced cookie jar at rest -- putting it in `project.json` env defeats the purpose (it lands in the attested, readable source), and the only existing escape (`env_passthrough`) is image-runtime only. This forces a choice between {source-hash attestation + deno handler} and {non-committed secret + image runtime}; you can't have both.
**Fix:** Mirror the `start_image()` loop in `start_isolated()`: before building the deno argv, fold `env_passthrough` (resolved from `os.environ`) into the env dict that becomes `ctx.env`. ~4 lines, reuses the existing convention. Stronger follow-up: derive a per-app key from the dstack `GetKey` the daemon already holds (ingress.py ~722), HKDF it, and inject as `ctx.env.SEAL_KEY` -- so the secret is per-app and TEE-bound rather than a shared daemon-env value.

---

## RUNTIME / RECOVERY

### 14. Image apps don't retry on boot; crashed containers aren't restarted (listen boot-order race)
**Severity:** Medium
**Where:** `proxy/runtimes.py` `recover_all()` / `start_image()`; affects any app with a startup dependency.
**Problem:** On a CVM restart `recover_all()` starts all project containers with no ordering and no restart policy. An image app that does a startup-time dependency call -- e.g. `listen` signs into `tinycloud` on boot -- can start before its dependency is ready, fail, `exit(1)`, and stay down (no retry, no restart). Observed 2026-06-24: after a daemon redeploy `listen-attested` exited at "Signing in to TinyCloud..." while `tinycloud` was still booting, and did not self-recover. Manual `POST /_api/projects/listen/redeploy` (once tinycloud was up) restored it.
**Fix options:**
- (a) Give daemon-managed app containers a Docker restart policy (`on-failure`/`unless-stopped`) so a crashed app retries.
- (b) `recover_all()` retries failed image starts with backoff, and/or health-gates dependent apps.
- (c) Apps own their startup resilience (retry the dependency) -- but the platform shouldn't depend on that.
**Related (fixed in this pass):** `_api_redeploy()` was dropping `oci_runtime` when reconstructing the manifest, so redeploying a runc-only app (listen/tinycloud) would relaunch it under gVisor and re-break it (gVisor can't reach Docker's embedded DNS). Now preserved -- which is what made the manual restore above work.

---

## PRIORITY RECOMMENDATION

**Must fix for v0.2:**
1. Issue #2 -- WebSocket/streaming in ingress (unblocks tunnel perf + real apps)
2. Issue #4 -- Deno hot-reload or at least reliable restart (unblocks dev iteration)
3. Issue #1 -- X-Forwarded-For (basic proxy hygiene)

**Should fix:**
4. Issue #10 -- Health checks after deploy
5. Issue #5 -- Consistent deploy API format
6. Issue #3 -- Request logging

**Nice to have:**
7-12. Everything else

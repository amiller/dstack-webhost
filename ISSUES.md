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
**Where:** `apps/tunnel/server.ts` + `apps/tunnel/client.ts`
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

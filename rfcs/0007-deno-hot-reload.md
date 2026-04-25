# RFC 0007: Deno Runtime Hot-Reload

## Summary
Make the Deno runtime container detect new/changed project files and reload modules without requiring a full container restart.

## Problem
When a new project is deployed via multipart upload, files are written to the shared volume but the running deno container's `import()` caches the old module. `rtm.refresh()` recreates the container, but only when the LAST project using that runtime is torn down. Adding a new project to an already-running container does nothing — the router's initial module scan already ran.

Current workaround: tear down ALL projects on that runtime, wait for container to die, then redeploy everything. Very painful for iteration.

## Files to Modify
- Deno router module (handles module loading and routing)
- Possibly `proxy/runtimes.py` for signaling

## Implementation
Pick ONE of these approaches (recommended: option A):

**Option A: `Deno.watchFs()` with hot-reload (recommended)**
1. In the Deno router, after initial module scan, start `Deno.watchFs()` on the projects directory
2. On file change event, debounce 500ms (to batch rapid writes), then re-import the changed module
3. If the new module fails to import, keep the old version running and log the error
4. Send a signal back to the ingress that the reload completed (optional)

**Option B: `/__reload` HTTP endpoint**
1. Add a `/_reload` endpoint to the Deno router
2. When hit, re-scan the projects directory and re-import all modules
3. The deploy endpoint in `proxy/deploy.py` calls `/_reload` after writing files
4. Simpler but requires the deployer to know about it

**Option C: Always restart on deploy**
1. Simplest: `refresh()` always restarts the container after any deploy
2. Downside: all projects on that runtime go down briefly during restart
3. Acceptable if restart is fast (< 2s)

## Testing & Validation Requirements
- Deploy project A to a fresh runtime. Verify it works.
- Deploy project B to the SAME runtime (without teardown). Verify B works AND A still works.
- Modify a file in project A. Verify the change is picked up without any manual restart.
- Deploy a broken module (syntax error). Verify the old version still serves requests (no crash).
- Deploy to 3 projects simultaneously. Verify all 3 are routable within 5 seconds.
- Performance: verify the reload/watch doesn't add measurable latency to normal request serving.

## Report Requirements
- Which approach was chosen and why
- Show the code changes with before/after diff
- Test transcript showing deploy → verify → modify → verify cycle
- Timing measurements: how long from file write to new code serving requests
- Edge cases documented: what happens on syntax errors, circular imports, deleted files

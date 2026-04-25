# RFC 0009: Runtime Container Health Checks

## Summary
Add a `/_health` endpoint to each runtime container and have the ingress poll it after deploy before reporting success.

## Problem
After deploying, the ingress assumes the runtime is up. No readiness probe or health check. If the deno container crashes on startup (e.g., bad module import), the ingress happily 502s with no diagnostic.

## Files to Modify
- Deno router — add `/_health` endpoint
- `proxy/runtimes.py` — poll health after deploy
- `proxy/deploy.py` — await health check before returning success

## Implementation
1. Add a `/_health` endpoint to the Deno router that returns 200 OK with `{"status":"ok","uptime":<seconds>}` when the router is ready to serve requests
2. In `proxy/runtimes.py`, after `refresh()` starts a container, poll `/_health` with exponential backoff:
   - First poll after 500ms
   - Retry up to 10 times with 500ms, 1s, 1s, 2s, 2s, 4s... intervals
   - Total timeout: 30 seconds
3. If health check passes, return success
4. If health check fails after 30s, return a clear error with the container logs
5. In `proxy/deploy.py`, await the health check result before returning 200 to the caller
6. For existing containers (no restart needed), skip the health check (already healthy)

## Testing & Validation Requirements
- Deploy a healthy app. Verify the deploy response comes AFTER the health check passes (not before).
- Deploy an app with a syntax error. Verify the deploy returns 500 with useful error message, not 200.
- Verify health endpoint returns correct uptime (increases over time).
- Verify health check timeout works: deploy to a container that never starts, verify it fails after ~30s.
- Verify existing running containers are NOT re-health-checked on redeploy of a different project.

## Report Requirements
- Show the health endpoint code and the polling logic
- Test transcript for healthy deploy showing timing
- Test transcript for failing deploy showing the error message
- Diff of runtimes.py and deploy.py

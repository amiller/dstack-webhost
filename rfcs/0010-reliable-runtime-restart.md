# RFC 0010: Reliable Runtime Restart on Teardown

## Summary
Fix `rtm.refresh()` to always restart the runtime container when there are remaining projects, ensuring redeployed code gets picked up.

## Problem
If project A and B share a runtime, tearing down A leaves the container running for B. Redeploying A writes new files but the container never restarts, so old cached code keeps running. The container only restarts when the LAST project is torn down.

## Files to Modify
- `proxy/runtimes.py` — the `refresh()` method

## Implementation
1. In `refresh()`, after removing a project and finding there are still remaining projects:
   - Log a warning that a restart is needed to pick up changes
   - Restart the container anyway (not just when the last project is removed)
   - On restart, all remaining projects get re-imported
2. Add a `force_restart` parameter to `refresh()` that can be called from the deploy endpoint
3. After restart, run health check (RFC 0009) to verify all remaining projects are still serving
4. Add a brief grace period: stop accepting new requests, drain in-flight requests, then restart

## Testing & Validation Requirements
- Deploy project A and B to the same runtime. Verify both work.
- Redeploy project A (change a file). Verify the new code is active within 10 seconds.
- Verify project B is still working after the restart (no data loss, no 502s beyond brief restart window).
- Tear down project A. Verify B still works and A is no longer routable.
- Tear down the last project. Verify the container is fully stopped (not just idle).

## Report Requirements
- Diff of `refresh()` with explanation of the logic change
- Test transcript showing the deploy → redeploy → verify cycle with timing
- Note any observed downtime during restart (should be < 5s)

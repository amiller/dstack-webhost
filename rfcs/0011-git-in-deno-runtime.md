# RFC 0011: Git Support in Deno Runtime Container

## Summary
Add `git` to the Deno runtime container image so git-clone-based deploys work.

## Problem
Git-clone-based deploys silently fail because the deno container doesn't have `git` installed. The deploy succeeds (files dir created) but the files are empty. No error is reported.

## Files to Modify
- Deno Dockerfile (the one that builds the runtime image)

## Implementation
1. Add `RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*` to the Deno runtime Dockerfile
2. In the deploy code that handles git clone: check the exit code of `git clone` and return a clear error if it fails
3. Add a validation step after clone: verify the target directory is non-empty (catches "clone succeeded but empty repo" case)
4. Rebuild the Docker image and update the running container

## Testing & Validation Requirements
- Deploy via git clone: use a small public repo URL. Verify files appear in the container.
- Deploy via git clone with a bad URL. Verify a clear error is returned (not silent empty dir).
- Deploy via git clone with a private repo (no auth). Verify a clear auth error.
- Verify the existing file-upload deploy path still works (no regression).
- Verify the git binary is available: `docker exec <runtime> which git` returns a path.

## Report Requirements
- Show the Dockerfile change
- Show the error handling code added to the deploy path
- Test transcript for successful git clone deploy
- Test transcript for failed git clone deploy (bad URL)

# RFC 0008: Consistent Multipart Deploy API

## Summary
Standardize the deploy API to always use multipart/form-data with both manifest JSON and file fields, resolving the current mismatch between the running daemon and the repo code.

## Problem
The running daemon on the CVM expects `multipart/form-data` with a `manifest` field (JSON string) and file fields. The newer code in the repo expects a JSON body for the manifest. This mismatch makes it impossible to deploy to the running daemon using the repo's own client code.

## Files to Modify
- `proxy/deploy.py` — reconcile the two versions
- Any client code that calls the deploy endpoint

## Implementation
1. Audit both versions of `deploy.py` — the one running on CVM and the one in the repo
2. Choose multipart/form-data as the canonical format (more flexible — can include files inline)
3. Update the repo version to match the running daemon's expectations:
   - `manifest` field: JSON string with project name, runtime type, ports, env vars
   - `files` field(s): uploaded files (tar, zip, or individual files)
4. Add validation: reject requests missing required fields with clear error messages
5. Add backward compatibility: if a JSON body is sent (old format), wrap it in the multipart format internally
6. Document the API contract in a docstring on the handler

## Testing & Validation Requirements
- Deploy with multipart: `curl -F "manifest=@manifest.json" -F "files=@app.tar" https://<ingress>/deploy`. Verify success.
- Deploy with JSON body (backward compat): `curl -H "Content-Type: application/json" -d '{"name":"test",...}' https://<ingress>/deploy`. Verify it still works.
- Deploy with missing manifest: `curl -F "files=@app.tar" https://<ingress>/deploy`. Verify a clear 400 error.
- Deploy with malformed JSON in manifest. Verify a clear 400 error.
- Verify the deployed app is actually running and routable after deploy.

## Report Requirements
- Side-by-side comparison of old API vs new API format
- Complete diff of deploy.py
- curl examples for every valid and invalid request type
- Error message samples

# RFC 0013: Tunnel Authentication Header-Based Auth

## Summary
Move tunnel authentication from URL path parameters to HTTP headers, preventing token leakage in browser history, server logs, and referrer headers.

## Problem
The tunnel ID (authentication token) is in the URL path. This means it appears in browser history, server logs, referrer headers. While the 64-char hex token is unguessable, URL-based auth is a bad practice.

## Files to Modify
- `apps/tunnel/server.ts` — accept auth via header
- `apps/tunnel/client.ts` — send auth via header

## Implementation
1. Add support for `Authorization: Bearer <tunnel-id>` header as an alternative to the URL path parameter
2. Poll and relay endpoints check the header first, fall back to URL parameter
3. For the WebSocket upgrade (RFC 0012), pass the token as a query parameter on the initial upgrade request only (this is standard for WebSocket auth since headers aren't supported in the browser WebSocket API)
4. Document the new auth method
5. Deprecation plan: log a warning when URL-based auth is used, remove in next major version

## Testing & Validation Requirements
- Auth via header: `curl -H "Authorization: Bearer <tunnel-id>" https://<tunnel>/poll`. Verify it works.
- Auth via URL (backward compat): `curl https://<tunnel>/<tunnel-id>/poll`. Verify it still works.
- No auth: `curl https://<tunnel>/poll`. Verify 401.
- Wrong token: `curl -H "Authorization: Bearer wrong"`. Verify 401.
- Verify the tunnel ID does NOT appear in server access logs when header-based auth is used.

## Report Requirements
- Diff of server.ts and client.ts
- Test commands for both auth methods
- Deprecation timeline

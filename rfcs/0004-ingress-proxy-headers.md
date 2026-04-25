# RFC 0004: Ingress Proxy Headers

## Summary
Add standard proxy headers (X-Forwarded-For, X-Forwarded-Proto) to the ingress proxy so downstream runtimes and apps can see the real client IP and protocol.

## Problem
The ingress in `proxy/ingress.py` `_proxy()` does not add `X-Forwarded-For` or `X-Real-IP` when proxying to runtimes. The deno runtime sees `host: 172.19.0.3:3000` (internal docker IP) and has zero client IP info. This breaks IP-based logic, rate limiting, geolocation, and audit logging.

## Files to Modify
- `proxy/ingress.py` — the `_proxy()` method around line ~98

## Implementation
In `_proxy()`, before forwarding the request to the upstream runtime:

1. Set `headers["X-Forwarded-For"]` to the request's remote address (request.remote or request.peername)
2. Set `headers["X-Forwarded-Proto"]` based on whether the original request was HTTP or HTTPS
3. Set `headers["X-Real-IP"]` to the same remote address
4. If X-Forwarded-For already exists (from an upstream proxy), append to it rather than replacing

## Testing & Validation Requirements
- Deploy a test app that returns all headers as JSON
- Send a request through the ingress and verify X-Forwarded-For, X-Real-IP, and X-Forwarded-Proto are present
- Verify the values match the actual client IP (not the internal docker IP)
- Test with curl: `curl -H "X-Forwarded-For: 1.2.3.4" https://<ingress>/test` should append, not replace
- Verify existing proxy behavior is unchanged (no regressions in routing, no dropped headers)

## Report Requirements
- Show the exact lines changed with before/after diff
- Include the curl test commands and their output proving the headers are present
- Note any edge cases (IPv6, multiple proxies in chain, missing remote addr)

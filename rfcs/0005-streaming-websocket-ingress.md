# RFC 0005: Streaming and WebSocket Support in Ingress

## Summary
Rewrite the ingress proxy to support bidirectional streaming and WebSocket upgrade, replacing the current request-buffer-respond pattern.

## Problem
`_proxy()` in `proxy/ingress.py` (lines 100-111) does `await request.read()` then `await resp.read()` and returns a single `web.Response`. This means:
- No 101 upgrade handling (WebSocket impossible)
- No chunked/streaming response (SSE, large downloads impossible)
- Entire request/response buffered in memory
- Current tunnel workaround uses long-polling with 25s timeouts, adding ~500ms latency per request

## Files to Modify
- `proxy/ingress.py` — complete rewrite of `_proxy()` method

## Implementation
1. Detect `Connection: Upgrade` / `Upgrade: websocket` headers at the top of the handler
2. For WebSocket requests: establish a bidirectional pipe between client and upstream using `aiohttp.web.WebSocketResponse`
3. For normal requests: use `aiohttp.web.StreamResponse` instead of buffering the full response
4. Stream the request body to upstream chunk-by-chunk (don't buffer)
5. Stream the response body back chunk-by-chunk
6. Handle backpressure properly — pause reading if the write buffer is full
7. Handle `Transfer-Encoding: chunked` responses
8. Handle `Content-Encoding: gzip/deflate` — pass through without decompressing
9. Support `Accept-Encoding: text/event-stream` for SSE passthrough

## Testing & Validation Requirements
- **Basic HTTP:** curl a simple GET through the ingress, verify identical response to direct access
- **Streaming response:** curl an endpoint that returns data slowly (e.g., a counter that increments every second). Verify data arrives incrementally, not all at once at the end
- **Large upload:** POST a 10MB file through the ingress. Verify it succeeds and the file is intact on the other side
- **WebSocket:** Connect a simple ws client through the ingress. Send messages both directions. Verify they arrive in order with no corruption
- **SSE:** Subscribe to an event stream through the ingress. Verify events arrive as they're emitted, not buffered
- **Connection drop:** Kill the upstream mid-stream. Verify the ingress returns an appropriate error to the client (not hanging forever)
- **Performance:** Compare latency before/after with a simple ping endpoint. Should be equal or better (no buffering overhead)

## Report Requirements
- Architecture diagram showing the new streaming data flow
- Complete diff of `proxy/ingress.py` with annotations on each section
- Latency benchmarks: before (buffered) vs after (streaming) for various payload sizes
- WebSocket test transcript showing bidirectional messages
- SSE test transcript showing live events
- Memory usage comparison (should be lower since no buffering)

## Priority
This is the single biggest limitation of the current platform. Unblocks tunnel performance (#8) and real-time applications.

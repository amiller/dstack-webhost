# RFC 0012: Tunnel Latency Optimization

## Summary
Reduce tunnel relay latency from ~500ms to sub-100ms by switching from long-polling to a WebSocket or SSE-based relay mechanism.

## Problem
Every visitor request goes through: visitor → ingress → tunnel queues request → client polls (500ms interval) → client fetches localhost → client POSTs relay → tunnel resolves promise → response goes back. Best case 500ms+ per request. Blocked by RFC 0005 (streaming support in ingress).

## Files to Modify
- `apps/tunnel/server.ts` — switch from poll-based relay to WebSocket/SSE
- `apps/tunnel/client.ts` — connect via WebSocket instead of polling
- Depends on RFC 0005 for ingress WebSocket support

## Implementation
**Do NOT start this until RFC 0005 (streaming/websocket ingress) is complete.**

1. Replace the poll/relay HTTP endpoints with a WebSocket connection between tunnel client and server
2. The client establishes a persistent WebSocket to the tunnel server on startup
3. When a visitor request arrives at the tunnel server, it sends the request over the WebSocket to the client
4. The client processes the request locally and sends the response back over the same WebSocket
5. The tunnel server relays the response back to the visitor
6. Add reconnection logic: if the WebSocket drops, the client reconnects with exponential backoff
7. Add heartbeat: ping/pong every 30s to detect dead connections
8. Keep the old poll/relay endpoints as a fallback for environments that don't support WebSocket

## Testing & Validation Requirements
- Measure round-trip latency before (polling) and after (WebSocket) for a simple echo endpoint
- Verify latency is under 100ms for local connections
- Test reconnection: kill the WebSocket server, verify client reconnects within 10s
- Test with concurrent requests: 10 simultaneous visitors, verify all get correct responses
- Test with large payloads: 1MB response through the tunnel
- Test fallback: disable WebSocket, verify polling still works

## Report Requirements
- Latency comparison: before vs after (include raw numbers)
- Architecture diagram showing the new WebSocket relay flow
- Reconnection behavior transcript
- Diff of server.ts and client.ts

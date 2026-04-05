# RFC 0002: Multi-Port and Protocol Routing

**Status**: Draft

## Problem

Currently all traffic enters through port 8080 and is routed by URL path
prefix. This works for HTTP apps but doesn't cover:

- Services that need their own port (databases, game servers, custom protocols)
- TCP services that aren't HTTP (SSH, MQTT, raw sockets)
- UDP services (DNS, game servers, VPN)
- Apps that expect to be the only thing on a port

## Proposal

### Port-based routing

dstack CVMs expose ports as subdomains:
`<app-id>-<port>.dstack-pha-prod7.phala.network`

The daemon should support binding projects to specific host ports, not just
path prefixes. A project manifest could look like:

```json
{
  "name": "my-api",
  "runtime": "deno",
  "entry": "server.ts",
  "listen": {
    "port": 3000,
    "protocol": "http"
  }
}
```

The daemon would expose port 3000 and route all traffic on that port to the
project's runtime container. Multiple projects can coexist, each on their
own port.

### TCP routing

For non-HTTP TCP services:

```json
{
  "name": "my-socket",
  "runtime": "dockerfile",
  "listen": {
    "port": 5000,
    "protocol": "tcp"
  }
}
```

The daemon proxies raw TCP connections to the container. No HTTP parsing,
just bidirectional byte streaming.

### UDP routing (future)

UDP is harder because there's no connection to track. Possible approaches:

- **Per-port UDP proxy**: Simple packet forwarding to a container
- **Built-in VPN adapter**: Run a WireGuard or similar endpoint inside the
  daemon, give each project its own virtual network interface. This would
  allow full UDP (and any other protocol) to reach containers.

The VPN approach is more complex but solves the general case. Worth
exploring once the TCP/HTTP routing is solid.

### Routing table

The daemon maintains a routing table:

| Host port | Protocol | Project   | Backend            |
|-----------|----------|-----------|--------------------|
| 8080      | http     | (ingress) | path-based routing |
| 3000      | http     | my-api    | deno runtime       |
| 5000      | tcp      | my-socket | container:5000     |
| 51820     | udp      | vpn       | wireguard adapter  |

Port 8080 remains the default ingress with path-based routing. Additional
ports are opt-in per project.

### Manifest changes

The `listen` field is optional. If omitted, the project is only accessible
via path-based routing on port 8080 (current behavior).

```json
{
  "listen": null,                          // path-only on :8080 (default)
  "listen": {"port": 3000},                // HTTP on :3000
  "listen": {"port": 5000, "protocol": "tcp"},  // raw TCP on :5000
  "listen": {"port": 5353, "protocol": "udp"}   // UDP on :5353 (future)
}
```

## Implementation Notes

- The ingress server needs to support binding multiple ports at startup
- For TCP routing, aiohttp's raw TCP handling or a separate listener per port
- Port conflicts should be validated at deploy time
- The dstack CVM's docker-compose needs to expose all declared ports
- The management API should expose the routing table: `GET /_api/routes`

### WebSocket support

The current ingress proxy in `proxy/ingress.py` does NOT support WebSocket
upgrades. It eagerly reads the full request/response body and returns a
`web.Response`, which is fundamentally incompatible with the 101 upgrade
handshake and persistent bidirectional connection that WebSocket requires.

To fix this, `_proxy()` needs to:

1. Check for `Upgrade: websocket` headers on the incoming request
2. Return a `web.WebSocketResponse` (aiohttp's WS type) instead of `web.Response`
3. Relay frames bidirectionally between the visitor and the backend

This is needed for apps like the tunnel (RFC 0003) which currently uses
long-polling as a workaround. See also `apps/tunnel/` for the long-poll
implementation that works without daemon changes.

## Out of Scope

- TLS termination (dstack handles this at the edge)
- Load balancing between multiple containers of the same project

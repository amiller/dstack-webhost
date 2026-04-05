# RFC 0003: Temporary Proxied Access

**Status**: Draft

## Problem

Sometimes you want to give someone access to a service running inside your
CVM without deploying it as a permanent project. Examples:

- "Let me show you this demo running on my machine" -- tunnel a local dev
  server through the CVM so a remote client can hit it, with TEE attestation
  proving the traffic path
- Temporary sharing of an internal service (database admin panel, metrics
  dashboard) with someone outside the CVM
- One-time collaborative sessions where two parties connect through the
  attested proxy

The key property: the proxy runs inside the TEE, so the traffic path is
attestable even though the origin service is ephemeral.

## Proposal

### Temporary tunnels

An API call creates a short-lived reverse tunnel:

```
POST /_api/tunnels
{
  "backend": "ws://user-machine:3000",    // or any reachable address
  "timeout": 3600,                          // seconds, max 86400
  "auth": "bearer",                         // auth mode for visitors
}
```

Response:

```json
{
  "id": "t-abc123",
  "url": "https://<cvm>/t/t-abc123/",
  "expires_at": "2026-04-05T12:00:00Z",
  "secret": "share-this-with-visitors"
}
```

The daemon proxies requests at `/<tunnel-id>/...` to the backend for the
duration specified. After timeout, the route is removed automatically.

### WebSocket relay

For interactive sessions (terminals, collaborative editors, VNC), the tunnel
supports WebSocket upgrade. The daemon acts as a transparent relay -- no
inspection, just forwarding bytes through the attested path.

### Attestation of the proxy itself

Since the daemon runs inside the CVM with dstack attestation, visitors can:

1. Fetch the CVM's attestation quote to verify they're talking to a real TEE
2. Check the daemon's source hash against the dstack-webhost repo
3. Know their traffic is passing through an attested proxy, not a MITM

This doesn't attest the backend service (that's on the developer's machine),
but it does attest the proxy layer -- useful for proving "this traffic wasn't
tampered with in transit through the CVM."

### Use cases

1. **Demo sharing**: Run your app locally, tunnel it through your CVM, share
   the URL. Visitors see it on your attested domain.
2. **Temporary access to internal services**: Expose a database admin panel
   or monitoring dashboard for a few hours without deploying it permanently.
3. **Verified relay**: Two parties who distrust each other can agree to
   route traffic through an attested proxy. Neither party controls the relay,
   and the TEE guarantees the relay isn't modifying traffic.

### Lifecycle

```
POST   /_api/tunnels          create tunnel
GET    /_api/tunnels           list active tunnels
DELETE /_api/tunnels/<id>      revoke early
GET    /t/<id>/...             proxied traffic (any visitor)
```

Tunnels are intentionally ephemeral. If you want something permanent,
deploy it as a project (RFC 0001, Layer 2).

## Implementation Notes

- Backend connections: the daemon needs to be able to reach the backend.
  For local dev machines, this means either the developer's machine is
  reachable from the CVM, or the developer connects to the daemon via
  WebSocket first and the daemon relays through that.
- Two models:
  1. **Push** (daemon connects to backend): simpler, requires backend reachable
  2. **Pull** (developer connects to daemon via WS, daemon uses that as tunnel):
     works even if developer is behind NAT. Basically a reverse tunnel via WS.
- The pull model is probably more practical since CVMs don't have outbound
  network to arbitrary developer machines.
- Rate limiting and max concurrent tunnels should be configurable.

## Out of Scope

- Full VPN tunneling (see RFC 0002)
- Permanent tunnels (use project deployment)
- End-to-end encryption (the TEE provides transport integrity; if the backend
  is on the developer's machine, they should use HTTPS/WSS themselves)

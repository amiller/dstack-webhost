# tunnel

Temporary reverse proxy for dstack-webhost. Like ngrok, but the relay runs
inside your TEE-attested CVM.

Works through the existing HTTP-only ingress using long-polling. No daemon
changes needed.

## Usage

### Start the client on your machine

```bash
deno run --allow-net client.ts http://your-cvm:8080/tunnel/ http://localhost:3000
```

Output:
```
  Tunnel created!
  Secret:   a1b2c3d4e5f6
  Expires:  2026-04-05T16:00:00Z
  Visitor:  http://your-cvm:8080/tunnel/

Waiting for incoming requests...
```

### Share the visitor URL

Clients should pass the tunnel secret in an HTTP header so it does not appear in
browser history, server logs, or referrer headers:

```bash
curl http://your-cvm:8080/tunnel/api/items \
  -H "Authorization: Bearer a1b2c3d4e5f6"
```

The legacy URL form `http://your-cvm:8080/tunnel/a1b2c3d4e5f6/api/items`
continues to work for browser-only demos, but the server logs a deprecation
warning when it is used.

### The client logs each request

```
  GET / -> http://localhost:3000/
    <- 200
  POST /api/items -> http://localhost:3000/api/items
    <- 201
```

## How it works

1. Client POSTs to create a tunnel, gets back a secret
2. Client long-polls `/poll` with `Authorization: Bearer <secret>` waiting for visitor requests
3. Visitors hit `/<path>` with `Authorization: Bearer <secret>` -- requests are queued server-side
4. When the client picks up a request, it fetches the backend locally
5. Client POSTs the response back to `/relay` with `Authorization: Bearer <secret>`
6. Visitor gets the response

For backward compatibility, `/<secret>/poll`, `/<secret>/relay`, and
`/<secret>/<path>` are still accepted. URL-based tunnel authentication is
deprecated and should be removed in the next major version.

## Deploy as a Layer 2 project

```bash
curl -X POST http://your-cvm:8080/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "tunnel",
    "runtime": "deno",
    "entry": "server.ts",
    "source": "https://github.com/amiller/dstack-webhost",
    "ref": "main"
  }'
```

Then the tunnel app is live at `http://your-cvm:8080/tunnel/`.

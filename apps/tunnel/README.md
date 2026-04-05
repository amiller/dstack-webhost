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
  Visitor:  http://your-cvm:8080/tunnel/a1b2c3d4e5f6/

Waiting for incoming requests...
```

### Share the visitor URL

Anyone who visits the visitor URL gets their requests proxied through the
CVM to your local service, relayed via long-polling through the tunnel client.

### The client logs each request

```
  GET / -> http://localhost:3000/
    <- 200
  POST /api/items -> http://localhost:3000/api/items
    <- 201
```

## How it works

1. Client POSTs to create a tunnel, gets back a secret
2. Client long-polls `/<secret>/poll` waiting for visitor requests
3. Visitors hit `/<secret>/<path>` -- requests are queued server-side
4. When the client picks up a request, it fetches the backend locally
5. Client POSTs the response back to `/<secret>/relay`
6. Visitor gets the response

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

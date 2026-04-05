# tunnel

Temporary reverse proxy for dstack-webhost. Like ngrok, but the relay runs
inside your TEE-attested CVM.

## Pull mode (WebSocket relay, works behind NAT)

The developer connects to the tunnel app via WebSocket from their local
machine. The daemon relays incoming HTTP requests through that WebSocket.

```bash
# On the developer's machine:
deno run --allow-net client.ts ws://your-cvm:8080/tunnel/ --backend http://localhost:3000
```

This works even if the developer is behind NAT.

## Attestation

Since the tunnel runs inside the CVM, visitors can verify the CVM's
attestation quote and daemon source hash. The relay path is attested;
the backend service itself is not.

## Files

- `server.ts` -- Deno handler (deploy as Layer 2 project)
- `client.ts` -- CLI client for pull-mode tunneling

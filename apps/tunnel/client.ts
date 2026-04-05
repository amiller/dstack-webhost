#!/usr/bin/env deno run --allow-net --allow-env
/**
 * tunnel client - connects your local service to a dstack-webhost tunnel
 *
 * Usage:
 *   deno run --allow-net client.ts ws://<cvm-host>:8080/tunnel/ http://localhost:3000
 *
 * This opens a WebSocket to the tunnel app running on the CVM,
 * then relays incoming HTTP requests to your local service.
 */

const tunnelUrl = Deno.args[0];
const backendUrl = Deno.args[1];

if (!tunnelUrl || !backendUrl) {
  console.error("Usage: deno run --allow-net client.ts <ws://cvm-host:8080/tunnel/> <http://localhost:3000>");
  Deno.exit(1);
}

console.log(`Connecting to tunnel: ${tunnelUrl}`);
console.log(`Backend: ${backendUrl}`);

const ws = new WebSocket(tunnelUrl);

ws.addEventListener("open", () => {
  console.log("WebSocket connected, registering tunnel...");
  ws.send(JSON.stringify({ type: "register", backend: backendUrl }));
});

ws.addEventListener("message", async (event) => {
  const msg = JSON.parse(event.data);

  if (msg.type === "registered") {
    console.log(`\n  Tunnel active!`);
    console.log(`  ID:       ${msg.id}`);
    console.log(`  Secret:   ${msg.secret}`);
    console.log(`  URL:      ${msg.url}`);
    console.log(`  Expires:  ${msg.expiresAt}`);
    console.log(`\n  Visitors can access your service at:`);
    console.log(`  ${tunnelUrl.replace("ws://", "http://").replace("wss://", "https://").replace(/\/tunnel\/?$/, "")}/${msg.secret}/`);
    console.log();
    return;
  }

  if (msg.type === "request") {
    const { id, method, path, headers, body } = msg;
    const url = new URL(path, backendUrl);

    try {
      const resp = await fetch(url.toString(), {
        method,
        headers,
        body: body ? atob(body) : undefined,
      });

      const respBody = resp.body ? btoa(await resp.text()) : "";
      const respHeaders: Record<string, string> = {};
      resp.headers.forEach((v, k) => { respHeaders[k] = v; });

      ws.send(JSON.stringify({
        type: "response",
        id,
        status: resp.status,
        headers: respHeaders,
        body: respBody,
      }));
    } catch (err) {
      ws.send(JSON.stringify({
        type: "response",
        id,
        status: 502,
        headers: {},
        body: btoa(`Backend error: ${err.message}`),
      }));
    }
  }
});

ws.addEventListener("close", () => {
  console.error("Tunnel disconnected.");
  Deno.exit(1);
});

ws.addEventListener("error", (event) => {
  console.error("WebSocket error:", event);
  Deno.exit(1);
});

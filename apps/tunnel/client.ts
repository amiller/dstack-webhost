#!/usr/bin/env deno run --allow-net --allow-env
/**
 * tunnel client - connects your local service to a dstack-webhost tunnel
 *
 * Uses long-polling (works through HTTP-only ingress proxies).
 *
 * Usage:
 *   deno run --allow-net client.ts http://<cvm-host>:8080/tunnel/ http://localhost:3000
 */

const tunnelUrl = Deno.args[0];
const backendUrl = Deno.args[1];

if (!tunnelUrl || !backendUrl) {
  console.error("Usage: deno run --allow-net client.ts <http://cvm-host:8080/tunnel/> <http://localhost:3000>");
  Deno.exit(1);
}

console.log(`Tunnel server: ${tunnelUrl}`);
console.log(`Backend:       ${backendUrl}`);

// Create tunnel
const createResp = await fetch(tunnelUrl, { method: "POST" });
const tunnel = await createResp.json();
console.log(`\n  Tunnel created!`);
console.log(`  Secret:   ${tunnel.secret}`);
console.log(`  Expires:  ${tunnel.expiresAt}`);

// Build the visitor URL from the tunnel URL
const baseUrl = tunnelUrl.replace(/\/+$/, "");
const visitorUrl = `${baseUrl}/${tunnel.secret}/`;
const pollUrl = `${baseUrl}/${tunnel.secret}/poll`;
const relayUrl = `${baseUrl}/${tunnel.secret}/relay`;

console.log(`  Visitor:  ${visitorUrl}`);
console.log(`\nWaiting for incoming requests...\n`);

let lastSeq = 0;

async function pollLoop() {
  while (true) {
    try {
      const pollResp = await fetch(pollUrl, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ afterSeq: lastSeq }),
      });

      if (pollResp.status === 410) {
        console.error("Tunnel expired.");
        break;
      }

      const msg = await pollResp.json();

      if (msg.empty) continue;

      // Got an incoming request from a visitor
      const { id, method, path, headers, body: reqBodyB64, seq } = msg;
      lastSeq = seq;

      const reqBody = reqBodyB64 ? atob(reqBodyB64) : undefined;
      const url = new URL(path, backendUrl).toString();

      console.log(`  ${method} ${path} -> ${url}`);

      try {
        const resp = await fetch(url, {
          method,
          headers,
          body: reqBody,
        });

        const respBodyB64 = resp.body ? btoa(await resp.text()) : "";
        const respHeaders: Record<string, string> = {};
        resp.headers.forEach((v, k) => { respHeaders[k] = v; });

        await fetch(relayUrl, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            id,
            status: resp.status,
            headers: respHeaders,
            body: respBodyB64,
          }),
        });

        console.log(`    <- ${resp.status}`);
      } catch (err) {
        await fetch(relayUrl, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            id,
            status: 502,
            headers: { "content-type": "text/plain" },
            body: btoa(`Backend error: ${(err as Error).message}`),
          }),
        });
        console.log(`    <- 502 backend error`);
      }
    } catch (err) {
      console.error(`Poll error: ${(err as Error).message}, retrying in 3s...`);
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}

pollLoop();

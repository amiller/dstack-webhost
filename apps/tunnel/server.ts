// tunnel - temporary reverse proxy via dstack-webhost
// Deploys as a Layer 2 app. Creates short-lived tunnels
// where visitors can reach your local service through the CVM.
//
// Usage (as a deno runtime project on tee-daemon):
//   POST /_api/tunnels  -> create tunnel
//   GET  /t/<id>/...    -> proxied traffic

const tunnels = new Map<string, {
  id: string;
  backendUrl: string;
  ws: WebSocket | null;
  createdAt: number;
  expiresAt: number;
  secret: string;
  queue: ((chunk: Uint8Array) => void)[];
}>();

function genId(): string {
  return "t-" + crypto.randomUUID().slice(0, 8);
}

function genSecret(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
}

// Tunnel via "pull" model: developer connects via WebSocket to register,
// then the daemon relays incoming HTTP requests through that WS.

export default async function handler(req: Request, ctx: { env: Record<string, string> }): Promise<Response> {
  const url = new URL(req.url);
  const path = url.pathname;

  // WebSocket upgrade for tunnel registration/relay
  if (req.headers.get("upgrade") === "websocket") {
    const backendUrl = url.searchParams.get("backend") || "";
    const secret = url.searchParams.get("secret") || "";
    const timeout = parseInt(url.searchParams.get("timeout") || "3600");

    if (!backendUrl) {
      return new Response("missing ?backend=", { status: 400 });
    }

    // If secret provided, connect to existing tunnel (relay mode)
    if (secret) {
      const tunnel = tunnels.get(secret);
      if (!tunnel) {
        return new Response("tunnel not found", { status: 404 });
      }

      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);

      // Accept the client WS
      server.accept();

      // When we get data from the developer's WS, forward to pending requests
      let pendingResolve: ((resp: Response) => void) | null = null;

      server.addEventListener("message", (event) => {
        const data = typeof event.data === "string" ? event.data : event.data;
        // The developer sends back HTTP responses as JSON: { status, headers, body (base64) }
        try {
          const msg = JSON.parse(data as string);
          if (pendingResolve && msg.type === "response") {
            const body = msg.body ? atob(msg.body) : "";
            const resp = new Response(body, {
              status: msg.status || 200,
              headers: msg.headers || {},
            });
            pendingResolve(resp);
            pendingResolve = null;
          }
        } catch {
          // raw data, treat as response body
          if (pendingResolve) {
            pendingResolve(new Response(data as string, { status: 200 }));
            pendingResolve = null;
          }
        }
      });

      // Store the WS on the tunnel
      tunnel.ws = server as unknown as WebSocket;

      return new Response(null, { status: 101, webSocket: client });
    }

    // Register a new tunnel
    const id = genId();
    const tunnelSecret = genSecret();
    const maxTimeout = parseInt(ctx.env.MAX_TUNNEL_TIMEOUT || "86400");
    const expiresAt = Date.now() + Math.min(timeout, maxTimeout) * 1000;

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();

    const tunnel = {
      id,
      backendUrl,
      ws: server as unknown as WebSocket,
      createdAt: Date.now(),
      expiresAt,
      secret: tunnelSecret,
      queue: [],
    };
    tunnels.set(tunnelSecret, tunnel);

    // Send tunnel info back to the developer
    server.addEventListener("open", () => {
      server.send(JSON.stringify({
        type: "registered",
        id,
        secret: tunnelSecret,
        url: `/${id}/`,
        expiresAt: new Date(expiresAt).toISOString(),
      }));
    });

    server.addEventListener("close", () => {
      tunnels.delete(tunnelSecret);
    });

    // Auto-cleanup on expiry
    setTimeout(() => {
      tunnels.delete(tunnelSecret);
      try { server.close(); } catch {}
    }, Math.min(timeout, maxTimeout) * 1000);

    return new Response(null, { status: 101, webSocket: client });
  }

  // Management API
  if (path === "/api") {
    const list = [...tunnels.values()].map(t => ({
      id: t.id,
      backendUrl: t.backendUrl,
      createdAt: new Date(t.createdAt).toISOString(),
      expiresAt: new Date(t.expiresAt).toISOString(),
      connected: t.ws !== null,
    }));
    return new Response(JSON.stringify({ tunnels: list }, null, 2), {
      headers: { "content-type": "application/json" },
    });
  }

  // Check for tunnel route: /<secret>/<rest...>
  const parts = path.split("/").filter(Boolean);
  const maybeSecret = parts[0];

  const tunnel = tunnels.get(maybeSecret);
  if (!tunnel) {
    return new Response(
      `tunnel not found. POST management or connect via WebSocket.\nActive: ${tunnels.size}`,
      { status: 404, headers: { "content-type": "text/plain" } }
    );
  }

  if (Date.now() > tunnel.expiresAt) {
    tunnels.delete(maybeSecret);
    return new Response("tunnel expired", { status: 410 });
  }

  if (!tunnel.ws) {
    return new Response("tunnel backend not connected", { status: 503 });
  }

  // Relay the HTTP request through the WebSocket to the developer's machine
  const subpath = "/" + parts.slice(1).join("/") + (url.search || "");
  const reqBody = req.body ? btoa(await req.text()) : "";

  return new Promise<Response>((resolve) => {
    const msg = JSON.stringify({
      type: "request",
      method: req.method,
      path: subpath,
      headers: Object.fromEntries(req.headers.entries()),
      body: reqBody,
    });

    // Set up response handler
    const handler = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data as string);
        if (data.type === "response") {
          tunnel.ws!.removeEventListener("message", handler);
          const body = data.body ? atob(data.body) : "";
          resolve(new Response(body, {
            status: data.status || 200,
            headers: data.headers || {},
          }));
        }
      } catch {}
    };

    tunnel.ws!.addEventListener("message", handler);
    tunnel.ws!.send(msg);

    // Timeout after 30s
    setTimeout(() => {
      tunnel.ws!.removeEventListener("message", handler);
      resolve(new Response("gateway timeout", { status: 504 }));
    }, 30000);
  });
}

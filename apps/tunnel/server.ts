// tunnel - temporary reverse proxy via dstack-webhost (long-poll mode)
//
// Works through the existing HTTP-only ingress proxy. No WebSocket needed.
//
// Developer runs the client on their machine, which polls the tunnel
// app for incoming requests and posts responses back. Visitors hit
// the tunnel URL and their requests get queued for the client to pick up.
//
// Deploy as a deno runtime project on tee-daemon.
//
// Auth: creation requires TUNNEL_TOKEN env var as Bearer auth.
// The listing endpoint is removed -- you track your own secrets.

interface PendingRequest {
  id: string;
  method: string;
  path: string;
  headers: Record<string, string>;
  body: string; // base64
  resolve: (resp: { status: number; headers: Record<string, string>; body: string }) => void;
  timestamp: number;
}

interface Tunnel {
  secret: string;
  createdAt: number;
  expiresAt: number;
  pollSeq: number;
}

const tunnels = new Map<string, Tunnel>();
const pendingRequests = new Map<string, PendingRequest>();
const pendingSeqs = new Map<string, number>();

function genSecret(): string {
  // 256-bit random, hex encoded -- not guessable
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
}

function checkAuth(req: Request, token: string): boolean {
  if (!token) return true; // no token configured = open (dev mode)
  const auth = req.headers.get("authorization") || "";
  return auth === `Bearer ${token}`;
}

// Cleanup expired tunnels and stale requests every 10s
setInterval(() => {
  const now = Date.now();
  for (const [secret, tunnel] of tunnels) {
    if (now > tunnel.expiresAt) {
      tunnels.delete(secret);
    }
  }
  for (const [id, req] of pendingRequests) {
    if (now - req.timestamp > 60000) {
      req.resolve({ status: 504, headers: { "content-type": "text/plain" }, body: btoa("gateway timeout") });
      pendingRequests.delete(id);
    }
  }
}, 10_000);

export default async function handler(req: Request, ctx: { env: Record<string, string> }): Promise<Response> {
  const url = new URL(req.url);
  const parts = url.pathname.split("/").filter(Boolean);
  const maxTimeout = parseInt(ctx.env.MAX_TUNNEL_TIMEOUT || "86400");
  const token = ctx.env.TUNNEL_TOKEN || "";

  // POST /  -> create tunnel (requires auth)
  if (req.method === "POST" && parts.length === 0) {
    if (!checkAuth(req, token)) {
      return new Response(JSON.stringify({ error: "unauthorized" }), { status: 401, headers: { "content-type": "application/json" } });
    }
    const body = await req.json();
    const timeout = Math.min(parseInt(body.timeout || "3600"), maxTimeout) * 1000;

    const secret = genSecret();
    tunnels.set(secret, {
      secret,
      createdAt: Date.now(),
      expiresAt: Date.now() + timeout,
      pollSeq: 0,
    });

    return new Response(JSON.stringify({
      secret,
      pollUrl: `/${secret}/poll`,
      relayUrl: `/${secret}/relay`,
      visitorUrl: `/${secret}/`,
      expiresAt: new Date(Date.now() + timeout).toISOString(),
    }), {
      headers: { "content-type": "application/json" },
    });
  }

  // GET /  -> just shows status, no secrets
  if (req.method === "GET" && parts.length === 0) {
    return new Response(JSON.stringify({
      active: tunnels.size,
      pending: pendingRequests.size,
    }), {
      headers: { "content-type": "application/json" },
    });
  }

  // DELETE /<secret>  -> revoke tunnel (requires auth)
  if (req.method === "DELETE" && parts.length === 1) {
    if (!checkAuth(req, token)) {
      return new Response(JSON.stringify({ error: "unauthorized" }), { status: 401, headers: { "content-type": "application/json" } });
    }
    const secret = parts[0];
    tunnels.delete(secret);
    return new Response(JSON.stringify({ ok: true }), {
      headers: { "content-type": "application/json" },
    });
  }

  // POST /<secret>/poll  -> developer polls for next request (long-poll)
  if (req.method === "POST" && parts.length === 2 && parts[1] === "poll") {
    const secret = parts[0];
    const tunnel = tunnels.get(secret);
    if (!tunnel) {
      return new Response(JSON.stringify({ error: "tunnel not found" }), { status: 404, headers: { "content-type": "application/json" } });
    }
    if (Date.now() > tunnel.expiresAt) {
      tunnels.delete(secret);
      return new Response(JSON.stringify({ error: "tunnel expired" }), { status: 410, headers: { "content-type": "application/json" } });
    }

    const afterSeq = parseInt((await req.json()).afterSeq || "0");

    const deadline = Date.now() + 25_000;
    while (Date.now() < deadline) {
      for (const [id, req] of pendingRequests) {
        const seq = pendingSeqs.get(id);
        if (seq && seq > afterSeq) {
          return new Response(JSON.stringify({
            id: req.id,
            method: req.method,
            path: req.path,
            headers: req.headers,
            body: req.body,
            seq,
          }), {
            headers: { "content-type": "application/json" },
          });
        }
      }
      await new Promise(r => setTimeout(r, 500));
    }

    return new Response(JSON.stringify({ empty: true }), {
      headers: { "content-type": "application/json" },
    });
  }

  // POST /<secret>/relay  -> developer sends back a response
  if (req.method === "POST" && parts.length === 2 && parts[1] === "relay") {
    const secret = parts[0];
    if (!tunnels.has(secret)) {
      return new Response(JSON.stringify({ error: "tunnel not found" }), { status: 404, headers: { "content-type": "application/json" } });
    }

    const { id, status, headers, body } = await req.json();
    const pending = pendingRequests.get(id);
    if (!pending) {
      return new Response(JSON.stringify({ error: "request not found or already responded" }), { status: 404, headers: { "content-type": "application/json" } });
    }

    pendingRequests.delete(id);
    pendingSeqs.delete(id);
    pending.resolve({ status: status || 200, headers: headers || {}, body: body || "" });
    return new Response(JSON.stringify({ ok: true }), {
      headers: { "content-type": "application/json" },
    });
  }

  // Anything else: treat as visitor traffic -> /<secret>/<path...>
  if (parts.length >= 1) {
    const secret = parts[0];
    const tunnel = tunnels.get(secret);
    if (!tunnel) {
      return new Response("tunnel not found", { status: 404 });
    }
    if (Date.now() > tunnel.expiresAt) {
      tunnels.delete(secret);
      return new Response("tunnel expired", { status: 410 });
    }

    const visitorPath = "/" + (parts.length > 1 ? parts.slice(1).join("/") : "") + (url.search || "");
    const reqBody = req.body ? btoa(await req.text()) : "";

    const id = crypto.randomUUID().slice(0, 12);
    tunnel.pollSeq++;
    pendingSeqs.set(id, tunnel.pollSeq);

    const response = await new Promise<{ status: number; headers: Record<string, string>; body: string }>((resolve) => {
      pendingRequests.set(id, {
        id,
        method: req.method,
        path: visitorPath,
        headers: Object.fromEntries(req.headers.entries()),
        body: reqBody,
        resolve,
        timestamp: Date.now(),
      });

      setTimeout(() => {
        if (pendingRequests.has(id)) {
          pendingRequests.delete(id);
          pendingSeqs.delete(id);
          resolve({ status: 504, headers: { "content-type": "text/plain" }, body: btoa("gateway timeout") });
        }
      }, 30_000);
    });

    const respBody = response.body ? atob(response.body) : "";
    return new Response(respBody, {
      status: response.status,
      headers: response.headers,
    });
  }

  return new Response("tunnel: POST / to create (auth required), /<secret>/poll and /<secret>/relay for client", {
    headers: { "content-type": "text/plain" },
  });
}

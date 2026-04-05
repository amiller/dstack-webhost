
// tunnel - temporary reverse proxy via dstack-webhost (long-poll mode)
// Deploy as a deno runtime project on tee-daemon.
// Auth: creation requires TUNNEL_TOKEN env var as Bearer auth.

interface PendingRequest {
  id: string;
  method: string;
  path: string;
  headers: Record<string, string>;
  body: string;
  resolve: (resp: { status: number; headers: Record<string, string>; body: string }) => void;
  timestamp: number;
}

interface Tunnel {
  tid: string;
  createdAt: number;
  expiresAt: number;
  pollSeq: number;
}

const tunnels = new Map<string, Tunnel>();
const pendingRequests = new Map<string, PendingRequest>();
const pendingSeqs = new Map<string, number>();

function genTunnelId(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
}

function checkAuth(req: Request, tok: string): boolean {
  if (!tok) return true;
  return (req.headers.get("authorization") || "") === "Bearer " + tok;
}

setInterval(() => {
  const now = Date.now();
  for (const [k, t] of tunnels) {
    if (now > t.expiresAt) tunnels.delete(k);
  }
  for (const [id, r] of pendingRequests) {
    if (now - r.timestamp > 60000) {
      r.resolve({ status: 504, headers: { "content-type": "text/plain" }, body: btoa("gateway timeout") });
      pendingRequests.delete(id);
    }
  }
}, 10_000);

export default async function handler(req: Request, ctx: { env: Record<string, string> }): Promise<Response> {
  const url = new URL(req.url);
  const parts = url.pathname.split("/").filter(Boolean);
  const maxTimeout = parseInt(ctx.env.MAX_TUNNEL_TIMEOUT || "86400");
  const tok = ctx.env.TUNNEL_TOKEN || "";

  // POST / -> create tunnel (auth required)
  if (req.method === "POST" && parts.length === 0) {
    if (!checkAuth(req, tok)) {
      return new Response(JSON.stringify({ error: "unauthorized" }), { status: 401, headers: { "content-type": "application/json" } });
    }
    const body = await req.json();
    const timeout = Math.min(parseInt(body.timeout || "3600"), maxTimeout) * 1000;
    const tid = genTunnelId();
    tunnels.set(tid, { tid, createdAt: Date.now(), expiresAt: Date.now() + timeout, pollSeq: 0 });
    return new Response(JSON.stringify({
      tid,
      pollUrl: "/" + tid + "/poll",
      relayUrl: "/" + tid + "/relay",
      visitorUrl: "/" + tid + "/",
      expiresAt: new Date(Date.now() + timeout).toISOString(),
    }), { headers: { "content-type": "application/json" } });
  }

  // GET / -> status only
  if (req.method === "GET" && parts.length === 0) {
    return new Response(JSON.stringify({ active: tunnels.size, pending: pendingRequests.size }), { headers: { "content-type": "application/json" } });
  }

  // DELETE /<tid> -> revoke (auth required)
  if (req.method === "DELETE" && parts.length === 1) {
    if (!checkAuth(req, tok)) {
      return new Response(JSON.stringify({ error: "unauthorized" }), { status: 401, headers: { "content-type": "application/json" } });
    }
    tunnels.delete(parts[0]);
    return new Response(JSON.stringify({ ok: true }), { headers: { "content-type": "application/json" } });
  }

  // POST /<tid>/poll -> long-poll for requests
  if (req.method === "POST" && parts.length === 2 && parts[1] === "poll") {
    const t = tunnels.get(parts[0]);
    if (!t) return new Response(JSON.stringify({ error: "not found" }), { status: 404, headers: { "content-type": "application/json" } });
    if (Date.now() > t.expiresAt) { tunnels.delete(t.tid); return new Response(JSON.stringify({ error: "expired" }), { status: 410, headers: { "content-type": "application/json" } }); }
    const afterSeq = parseInt((await req.json()).afterSeq || "0");
    const deadline = Date.now() + 25_000;
    while (Date.now() < deadline) {
      for (const [id, r] of pendingRequests) {
        const seq = pendingSeqs.get(id);
        if (seq && seq > afterSeq) {
          return new Response(JSON.stringify({ id: r.id, method: r.method, path: r.path, headers: r.headers, body: r.body, seq }), { headers: { "content-type": "application/json" } });
        }
      }
      await new Promise(res => setTimeout(res, 500));
    }
    return new Response(JSON.stringify({ empty: true }), { headers: { "content-type": "application/json" } });
  }

  // POST /<tid>/relay -> send response back
  if (req.method === "POST" && parts.length === 2 && parts[1] === "relay") {
    const k = parts[0];
    if (!tunnels.has(k)) return new Response(JSON.stringify({ error: "not found" }), { status: 404, headers: { "content-type": "application/json" } });
    const { id, status, headers, body } = await req.json();
    const p = pendingRequests.get(id);
    if (!p) return new Response(JSON.stringify({ error: "already responded" }), { status: 404, headers: { "content-type": "application/json" } });
    pendingRequests.delete(id);
    pendingSeqs.delete(id);
    p.resolve({ status: status || 200, headers: headers || {}, body: body || "" });
    return new Response(JSON.stringify({ ok: true }), { headers: { "content-type": "application/json" } });
  }

  // Visitor traffic: /<tid>/<path...>
  if (parts.length >= 1) {
    const t = tunnels.get(parts[0]);
    if (!t) return new Response("not found", { status: 404 });
    if (Date.now() > t.expiresAt) { tunnels.delete(t.tid); return new Response("expired", { status: 410 }); }
    const visitorPath = "/" + (parts.length > 1 ? parts.slice(1).join("/") : "") + (url.search || "");
    const reqBody = req.body ? btoa(await req.text()) : "";
    const id = crypto.randomUUID().slice(0, 12);
    t.pollSeq++;
    pendingSeqs.set(id, t.pollSeq);
    const response = await new Promise<{ status: number; headers: Record<string, string>; body: string }>((resolve) => {
      pendingRequests.set(id, { id, method: req.method, path: visitorPath, headers: Object.fromEntries(req.headers.entries()), body: reqBody, resolve, timestamp: Date.now() });
      setTimeout(() => {
        if (pendingRequests.has(id)) { pendingRequests.delete(id); pendingSeqs.delete(id); resolve({ status: 504, headers: { "content-type": "text/plain" }, body: btoa("gateway timeout") }); }
      }, 30_000);
    });
    return new Response(response.body ? atob(response.body) : "", { status: response.status, headers: response.headers });
  }

  return new Response("tunnel: POST / to create, /<tid>/poll and /<tid>/relay for client", { headers: { "content-type": "text/plain" } });
}

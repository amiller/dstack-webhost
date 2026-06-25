
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

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}

function bearerToken(req: Request): string | undefined {
  const auth = req.headers.get("authorization") || "";
  return auth.startsWith("Bearer ") ? auth.slice("Bearer ".length).trim() : undefined;
}

function warnUrlAuth(): void {
  console.warn("URL-based tunnel authentication is deprecated. Use 'Authorization: Bearer <tid>' instead.");
}

async function readJson(req: Request): Promise<Record<string, unknown>> {
  const text = await req.text();
  return text ? JSON.parse(text) : {};
}

function getAuthedTunnel(req: Request, urlTid?: string): { tunnel?: Tunnel; response?: Response } {
  const headerTid = bearerToken(req);
  if (headerTid) {
    const tunnel = tunnels.get(headerTid);
    return tunnel ? { tunnel } : { response: json({ error: "unauthorized" }, 401) };
  }
  if (!urlTid) return { response: json({ error: "unauthorized" }, 401) };

  warnUrlAuth();
  const tunnel = tunnels.get(urlTid);
  return tunnel ? { tunnel } : { response: json({ error: "not found" }, 404) };
}

function checkExpiry(tunnel: Tunnel): Response | undefined {
  if (Date.now() <= tunnel.expiresAt) return undefined;
  tunnels.delete(tunnel.tid);
  return json({ error: "expired" }, 410);
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
      return json({ error: "unauthorized" }, 401);
    }
    const body = await readJson(req);
    const timeout = Math.min(parseInt(String(body.timeout || "3600")), maxTimeout) * 1000;
    const tid = genTunnelId();
    tunnels.set(tid, { tid, createdAt: Date.now(), expiresAt: Date.now() + timeout, pollSeq: 0 });
    return json({
      tid,
      pollUrl: "/poll",
      relayUrl: "/relay",
      visitorUrl: "/",
      expiresAt: new Date(Date.now() + timeout).toISOString(),
    });
  }

  // GET / -> status only
  if (req.method === "GET" && parts.length === 0 && !bearerToken(req)) {
    return json({ active: tunnels.size, pending: pendingRequests.size });
  }

  // DELETE /<tid> -> revoke (auth required)
  if (req.method === "DELETE" && parts.length === 1) {
    if (!checkAuth(req, tok)) {
      return json({ error: "unauthorized" }, 401);
    }
    tunnels.delete(parts[0]);
    return json({ ok: true });
  }

  // POST /poll (preferred) or /<tid>/poll (deprecated) -> long-poll for requests
  if (req.method === "POST" && ((parts.length === 1 && parts[0] === "poll") || (parts.length === 2 && parts[1] === "poll"))) {
    const urlTid = parts.length === 2 ? parts[0] : undefined;
    const auth = getAuthedTunnel(req, urlTid);
    if (auth.response) return auth.response;
    const t = auth.tunnel!;
    const expired = checkExpiry(t);
    if (expired) return expired;
    const body = await readJson(req);
    const afterSeq = parseInt(String(body.afterSeq || "0"));
    const deadline = Date.now() + 25_000;
    while (Date.now() < deadline) {
      for (const [id, r] of pendingRequests) {
        const seq = pendingSeqs.get(id);
        if (seq && seq > afterSeq) {
          return json({ id: r.id, method: r.method, path: r.path, headers: r.headers, body: r.body, seq });
        }
      }
      await new Promise(res => setTimeout(res, 500));
    }
    return json({ empty: true });
  }

  // POST /relay (preferred) or /<tid>/relay (deprecated) -> send response back
  if (req.method === "POST" && ((parts.length === 1 && parts[0] === "relay") || (parts.length === 2 && parts[1] === "relay"))) {
    const urlTid = parts.length === 2 ? parts[0] : undefined;
    const auth = getAuthedTunnel(req, urlTid);
    if (auth.response) return auth.response;
    const expired = checkExpiry(auth.tunnel!);
    if (expired) return expired;
    const payload = await readJson(req);
    const id = typeof payload.id === "string" ? payload.id : "";
    const status = typeof payload.status === "number" ? payload.status : 200;
    const headers = typeof payload.headers === "object" && payload.headers !== null ? payload.headers as Record<string, string> : {};
    const body = typeof payload.body === "string" ? payload.body : "";
    const p = pendingRequests.get(id);
    if (!p) return json({ error: "already responded" }, 404);
    pendingRequests.delete(id);
    pendingSeqs.delete(id);
    p.resolve({ status, headers, body });
    return json({ ok: true });
  }

  // Visitor traffic: Authorization header (preferred) or /<tid>/<path...> (deprecated)
  if (parts.length >= 1 || bearerToken(req)) {
    const headerTid = bearerToken(req);
    const auth = getAuthedTunnel(req, headerTid ? undefined : parts[0]);
    if (auth.response) return new Response(auth.response.status === 401 ? "unauthorized" : "not found", { status: auth.response.status });
    const t = auth.tunnel!;
    const expired = checkExpiry(t);
    if (expired) return new Response("expired", { status: 410 });
    const visitorPath = (headerTid ? url.pathname : "/" + (parts.length > 1 ? parts.slice(1).join("/") : "")) + (url.search || "");
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

// Issue #6 acceptance probe — runs INSIDE the tenant.
// Performs the broker calls the acceptance names and returns the transcript.
const SOCK = "/run/broker/dstack.sock";

async function rpc(method: string, body: Record<string, unknown>) {
  try {
    const conn = await Deno.connect({ transport: "unix", path: SOCK });
    const payload = JSON.stringify(body);
    const req =
      `POST /${method} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n` +
      `Content-Length: ${payload.length}\r\nConnection: close\r\n\r\n${payload}`;
    await conn.write(new TextEncoder().encode(req));
    const dec = new TextDecoder();
    let raw = "";
    const buf = new Uint8Array(8192);
    while (true) {
      const n = await conn.read(buf);
      if (n === null) break;
      raw += dec.decode(buf.subarray(0, n));
      if (raw.length > 262144) break;
    }
    conn.close();
    const idx = raw.indexOf("\r\n\r\n");
    const status = parseInt(raw.split(" ")[1] || "0", 10);
    return { status, body: raw.slice(idx + 4) };
  } catch (e) {
    return { status: 0, body: "", err: String(e) };
  }
}

export default async (_req: Request, _ctx: { env: Record<string, string> }) => {
  const out = {
    ts: new Date().toISOString(),
    sock: SOCK,
    // The acceptance call, in both dstack-proxy generations:
    // post-#80 proxies take {"name"} and derive the path; the pre-#80 proxy
    // takes {"path"} pinned under /tee-daemon/.
    getkey_name_mode: await rpc("GetKey", { name: "issue6-demo" }),
    getkey_path_mode: await rpc("GetKey", { path: "/tee-daemon/issue6-demo/data" }),
    // A non-allowlisted method must be denied BY THE BROKER (403), proving the
    // tenant is behind the filtered proxy, not a raw dstack socket.
    denied_method: await rpc("DeriveKey", {}),
  };
  return new Response(JSON.stringify(out, null, 2), {
    headers: { "content-type": "application/json" },
  });
};

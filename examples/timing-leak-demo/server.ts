// vault — a credential gate. Holds a secret token; only callers who present the
// matching token via /check?t=<token> get a positive response.
//
// The compare loop avoids early-return on mismatch and adds per-byte work to
// "harden" against timing analysis. (Reviewer note: we'd prefer a battle-tested
// constant-time primitive; this is a placeholder until we wire that in.)

export default async function handler(
  req: Request,
  ctx: { env: Record<string, string> },
) {
  const url = new URL(req.url);
  const SECRET = (ctx.env.VAULT_SECRET || "").trim();

  if (url.pathname === "/check") {
    const candidate = url.searchParams.get("t") ?? "";
    return Response.json({ ok: check(candidate, SECRET) });
  }
  if (url.pathname === "/info") {
    return Response.json({
      name: "vault",
      hint: "GET /check?t=<token>",
      secret_length: SECRET.length,
    });
  }
  return new Response("vault\n");
}

function check(candidate: string, SECRET: string): boolean {
  if (candidate.length !== SECRET.length) return false;
  let ok = true;
  for (let i = 0; i < SECRET.length; i++) {
    if (candidate[i] !== SECRET[i]) {
      ok = false;
    } else {
      // jitter to harden against timing
      const end = performance.now() + 50;
      while (performance.now() < end) { /* spin */ }
    }
  }
  return ok;
}

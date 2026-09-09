// RFC 0034 receiver: turns DAEMON_NOTIFY_HOOK envelopes into one message on the
// operator channel. The daemon POSTs {"event": "create"|"approve"|"freeze",
// "project": <name>, "status"?, "deadline"?, "created_by"?}; this handler validates
// the envelope, drops duplicates, and POSTs one m.text message to MATRIX_URL
// (the full Matrix room-send endpoint, with credentials — keep it in manifest.env,
// which the API always redacts). No fallbacks: a missing MATRIX_URL fails loudly.

const VALID_EVENTS = ["create", "approve", "freeze"];

// Bounded dedupe: the daemon is fire-and-forget, but a hook delivery retried after
// a restart must not double-post. Oldest keys fall off; 256 dwarfs any real queue.
const seen = new Set<string>();
function dedupe(key: string): boolean {
  if (seen.has(key)) return false;
  if (seen.size >= 256) seen.delete(seen.keys().next().value as string);
  seen.add(key);
  return true;
}

function body(event: string, e: any): string {
  if (event === "create") {
    const until = new Date(e.deadline * 1000).toISOString();
    return `project ${e.project} created by ${e.created_by}, pending until ${until}; approve: POST /_api/projects/${e.project}/approve`;
  }
  if (event === "approve") return `project ${e.project} approved`;
  return `project ${e.project} frozen (pending expired)`;
}

export default async function handler(req: Request, ctx?: { env: Record<string, string> }) {
  const url = new URL(req.url);
  if (req.method === "GET" && url.pathname === "/health") return new Response("ok\n");
  if (req.method !== "POST") return new Response("method not allowed\n", { status: 405 });

  let e: any;
  try {
    e = await req.json();
  } catch (_) {
    return Response.json({ error: "body is not valid JSON" }, { status: 400 });
  }
  if (!VALID_EVENTS.includes(e.event) || typeof e.project !== "string" || !e.project) {
    return Response.json({ error: `bad envelope: need event in ${JSON.stringify(VALID_EVENTS)} and a project name` }, { status: 400 });
  }
  if (e.event === "create" && (typeof e.deadline !== "number" || typeof e.created_by !== "string")) {
    return Response.json({ error: "bad create envelope: need numeric deadline and created_by" }, { status: 400 });
  }

  const key = JSON.stringify([e.event, e.project, e.deadline ?? null, e.created_by ?? null]);
  if (!dedupe(key)) return Response.json({ error: "duplicate event" }, { status: 409 });

  const matrix = ctx?.env.MATRIX_URL;
  if (!matrix) return Response.json({ error: "MATRIX_URL is not set" }, { status: 500 });

  const resp = await fetch(matrix, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ msgtype: "m.text", body: body(e.event, e) }),
  });
  if (!resp.ok) {
    return Response.json({ error: `matrix post failed: ${resp.status} ${await resp.text()}` }, { status: 502 });
  }
  return Response.json({ posted: body(e.event, e) });
}

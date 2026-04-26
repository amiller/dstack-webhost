# Developer guide

For deploying a project onto a tee-daemon CVM. For platform context, see the [homepage](index.md). For auditing a deployed project, see the [audit guide](audit.md).

You need:

- A running tee-daemon CVM and its admin token (`TEE_DAEMON_TOKEN`).
- Your project source in a public git repo, or a tarball of files.

## The handler contract

Your project is a single module that default-exports a request handler. The shared runtime loads it and calls it on every request to `/<project-name>/...`.

```ts
// server.ts (Deno)
export default async function handler(req: Request, ctx?: { env: Record<string,string>, dataDir: string }) {
  return new Response("hello");
}

// Optional: also run standalone for local dev
if (import.meta.main) Deno.serve({ port: 3000 }, handler);
```

The path the daemon receives (`/<name>/foo/bar`) is rewritten to `/foo/bar` before your handler sees it, so handlers don't need to know their mount point.

`ctx.env` is the env-var block from your manifest. `ctx.dataDir` is a per-project writable directory backed by a Docker volume; it survives runtime restarts but is not persisted across CVM redeploys, so treat it as a cache for things you can rebuild.

Other supported runtimes follow the same shape: a single entry file per project. Defaults are autodetected from the entry filename:

| Runtime | Entry | Notes |
|---|---|---|
| `deno` | `server.ts` | The example above. Bun shares this contract. |
| `node` | `index.js` | `package.json` honored if present. |
| `python` | `app.py` | `requirements.txt` honored if present. |
| `static` | `.` | A directory of files, served verbatim. |
| `dockerfile` | `Dockerfile` | Custom container; you provide the listener. |

For exact signatures of the non-Deno runtimes, see `proxy/runtimes.py` in the daemon repo — it's the source of truth.

## Deploy

From a public git repo:

```bash
TOKEN=...
CVM=https://your-cvm.dstack.phala.network

curl -X POST $CVM/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-app","source":"https://github.com/me/my-app","ref":"main"}'
```

From a local tarball (no public repo required):

```bash
tar czf app.tgz -C my-app .
curl -X POST $CVM/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -F 'manifest={"name":"my-app","runtime":"deno"};type=application/json' \
  -F "files=@app.tgz"
```

`mode` defaults to `dev`. The runtime is autodetected from the entry filename. Reach the running app at `$CVM/my-app/`.

## project.json (optional)

Drop this in the project's repo root to declare the runtime contract alongside the source:

```json
{
  "runtime": "deno",
  "entry": "server.ts",
  "mode": "dev",
  "env": { "DEBUG": "true" }
}
```

## Promote to attested

Promotion is the trust claim. The daemon records the source hash, opens the audit log, binds the hash into the TEE quote, and exposes the public verifier endpoints.

```bash
curl -X POST $CVM/_api/projects/my-app/promote -H "Authorization: Bearer $TOKEN"
```

Treat it like cutting a release — deliberate, not automatic. Subsequent redeploys append to the audit log; a counterparty walking the [verifier](verify.md) sees that a change happened and can decide whether to re-audit.

## Update or remove

```bash
# Re-pull from source (latest commit on the same ref)
curl -X POST $CVM/_api/projects/my-app/redeploy -H "Authorization: Bearer $TOKEN"

# Tear down
curl -X DELETE $CVM/_api/projects/my-app -H "Authorization: Bearer $TOKEN"
```

## API surface

Public (no auth required), only for **attested** projects:

| | |
|---|---|
| `GET /` | Listing of attested projects. `Accept: text/html` returns the daemon's viewer page; `Accept: application/json` returns JSON. |
| `GET /_api/projects/<name>` | Project manifest. |
| `GET /_api/projects/<name>/audit` | Audit log. |
| `GET /_api/attest/<name>` | Raw dstack quote. |
| `GET /_api/verification/<name>` | Manifest + quote + audit, in one response. |

Authenticated (`Authorization: Bearer $TOKEN`):

| | |
|---|---|
| `GET /_api/projects` | All projects, including dev. |
| `POST /_api/projects` | Deploy. |
| `POST /_api/projects/<name>/promote` | Dev → attested. |
| `POST /_api/projects/<name>/redeploy` | Re-pull from source. |
| `DELETE /_api/projects/<name>` | Tear down. |

## Where to look in the daemon

`proxy/ingress.py` has the request routing and auth gate. `proxy/runtimes.py` has the language-runtime container management and the Deno router that loads your handler. `proxy/deploy.py` has the git-clone path and the source-hash recording. The whole thing is small enough to read end-to-end.

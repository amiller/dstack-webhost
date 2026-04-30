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
| `image` | (none) | Layer-1 tenant — bring an existing OCI image. See [Image runtime](#image-runtime-layer-1). |

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

For deno/bun projects that want stronger sandboxing than the shared runtime, add `"isolation": "container"`. Each such project gets its own container running deno with `--allow-read` scoped to its own files, `--deny-env`, `--deny-ffi`, `--deny-run`, `--deny-sys`. `manifest.env` is passed via Deno args (not env permission) so handlers still see `ctx.env` but can't read other tenants' secrets. The container is placed on a per-project Docker network (`tee-proj-<name>-<mode>`), so siblings are not reachable by IP or container name. `ctx.dataDir` points at `/data`, backed by a per-project named volume — siblings' data is not visible.

## Image runtime (Layer 1)

For a tenant that ships as a built OCI image rather than a handler, use `runtime: "image"`:

```bash
curl -X POST $CVM/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-service",
    "runtime": "image",
    "image": "ghcr.io/me/my-service@sha256:...",
    "image_port": 8080,
    "volumes": [{"name": "my-service-data", "mount": "/var/lib/my-service"}],
    "env_passthrough": ["MY_API_KEY"]
  }'
```

| Field | Purpose |
|---|---|
| `image` | OCI reference. Pin by digest for attestable deploys. |
| `image_port` | Port the container listens on internally; ingress proxies path-based at `/<name>/`. |
| `volumes` | Optional `[{name, mount}]`. Named volumes are referenced by name and adopted idempotently — pre-existing data survives. |
| `env_passthrough` | Optional list of env-var names; the daemon forwards values from its own environment, keeping secrets out of `project.json`. |

The container runs under the daemon's configured OCI runtime (see `/_api/substrate`). On a CVM with `DAEMON_CONTAINER_RUNTIME=sysbox-runc`, all image-runtime tenants get user-namespace remap and virtualised `/proc` for free. The container is placed on a per-project Docker network — sibling tenants are not reachable by IP or hostname; only the daemon proxies traffic in and out. See the [isolation probe](isolation-probe.md) for a worked example.

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
| `GET /_api/substrate` | The substrate's runtime configuration: effective OCI runtime (e.g. `sysbox-runc`), supported isolation modes, deno entry-shim hash. Lets a relying party verify what's mediating tenant syscalls. |
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

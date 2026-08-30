# Developer guide

For deploying a project onto a tee-daemon CVM. For platform context, see the [homepage](index.md). For auditing a deployed project, see the [audit guide](audit.md).

Vocabulary, once: the **host owner** runs the CVM; an **app** (a `project` in the code and in the API below) is a deployed unit with its own container; a **user** (an *agent* in the Solid sense) is a person who delegates to apps. The substrate isolates apps from each other — user-from-user isolation lives in the apps' credential core, not in this daemon.

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
| `image` | (none) | Layer-1 app — bring an existing OCI image. See [Image runtime](#image-runtime-layer-1). |

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

For deno/bun projects that want stronger sandboxing than the shared runtime, add `"isolation": "container"`. Each such project gets its own container running deno with `--allow-read` scoped to its own files, `--deny-env`, `--deny-ffi`, `--deny-run`, `--deny-sys`. `manifest.env` is passed via Deno args (not env permission) so handlers still see `ctx.env` but can't read other projects' secrets. The container is placed on a per-project Docker network (`tee-proj-<name>-<mode>`), so siblings are not reachable by IP or container name. `ctx.dataDir` points at `/data`, backed by a per-project named volume — siblings' data is not visible.

## Image runtime (Layer 1)

For an app that ships as a built OCI image rather than a handler, use `runtime: "image"`:

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

The container runs under the daemon's configured OCI runtime (see `/_api/substrate`). On a CVM with `DAEMON_CONTAINER_RUNTIME=sysbox-runc`, all image-runtime apps get user-namespace remap and virtualised `/proc` for free. The container is placed on a per-project Docker network — sibling apps are not reachable by IP or hostname; only the daemon proxies traffic in and out. See the [isolation probe](isolation-probe.md) for a worked example.

### Multi-process workloads: fold them into one image

A project is **one container** — one handler for the language runtimes, one image for `runtime: "image"`. There is no manifest field for a second container, a sidecar, or a compose file; the daemon has no way to express one project spanning several containers. A workload of several cooperating services still deploys as one project by folding the services under a single supervisor **inside the image**, with one listening port for `image_port`.

The worked case is Port Call (`amiller/port-call`), a meeting bot whose rig is four services: `postgres`, a transcription shim, a TTS shim, and `vexa-lite` — itself already a supervisor monolith (redis, Xvfb, fluxbox, pulseaudio, x11vnc, websockify, and its APIs). **All four fold.** Nothing needs its own container for isolation: the substrate isolates projects *from each other*, and processes inside one project share one trust boundary anyway. The fold happens at image-build time because the manifest has no `command:` override — the supervisor must be the image's `ENTRYPOINT` — and `vexa-lite` already runs under a supervisor, so `postgres` and the two shims are three more programs in its config, not a new deployment unit.

What the fold owes the daemon:

| | |
|---|---|
| Durable data → named `volumes` | A redeploy stops and removes the container and starts a fresh one; anything not on a named volume is lost. (Port Call's recordings lived in the container's `/tmp` and were lost on every recreate — port-call#26.) Put the postgres data directory and the recordings on `volumes: [{name, mount}]`; volumes are adopted idempotently, so data survives both recreate and redeploy. |
| One port for `image_port` | Ingress proxies `/<name>/` to a single `image_port`. Services reach each other on container-internal ports; only the ingress-facing service needs to listen on `image_port`. |
| Crash handling | The container restarts on non-zero exit (`on-failure`, 5 retries); the supervisor restarts an individual crashed service without the container being replaced. |
| VPN routing | Two mechanisms, on different lines of this repo — see below. |
| Resource envelope | None: the daemon sets no per-container memory or CPU limits, so a folded stack shares the CVM's whole envelope with every other project. That is the honest cost of the fold, and the point at which to consider a dedicated CVM instead. |

**VPN routing.** Where the pod has a shared VPN egress network, set `egress: true` and the daemon joins the container to it and injects `EGRESS_PROXY_URL` / `ALL_PROXY` (`socks5://egress-vpn:1080`), so the app's outbound traffic routes through the pod's VPN. The VPN itself is another project — an attested image with `egress_provider: true` plus `cap_add: ["NET_ADMIN"]` and `devices: ["/dev/net/tun"]`. Caveat: this field landed on the `main` line (`d7fe947b`, 2026-07-03) and is not on the `staging` branch at the time of writing — a daemon built from `staging` silently ignores `egress`, so check `GET /_api/version` against your daemon before relying on it. The alternative every build carries: put your own VPN client in the image — `mode: "attested"` with the same `cap_add`/`devices` grant (rejected in dev mode; see RFC 0025).

What does *not* fold is not a service but the concerns around it: separate lifecycles, separate resource envelopes, and CVM-level isolation between the services themselves. When a workload genuinely needs those — or already has a compose file it wants to keep — run it on a dedicated CVM under compose instead (Port Call's `docker-compose.cvm.yml`, in its own repo, is that shape). That burns a whole CVM on one app, which is exactly what folding avoids; treat it as the escape hatch, not the default.

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
| `GET /_api/substrate` | The substrate's runtime configuration: effective OCI runtime (e.g. `sysbox-runc`), the runtimes Docker actually has (live `GET /info`), network-isolation posture (`host`/`sandbox`/`netns`), supported isolation modes, deno entry-shim hash. Lets a relying party verify what's mediating app syscalls — and see a configured/available mismatch rather than trust a name. |
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

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
| `image` | OCI reference. Pin by digest for attestable deploys. Public registries pull anonymously; for a **private** image, give the daemon registry creds (below) — no need to flip the package to public. |
| `image_port` | Port the container listens on internally; ingress proxies path-based at `/<name>/`. |
| `volumes` | Optional `[{name, mount}]`. Named volumes are referenced by name and adopted idempotently — pre-existing data survives. |
| `env_passthrough` | Optional list of env-var names; the daemon forwards values from its own environment, keeping secrets out of `project.json`. |

**Private registry pulls.** Set registry creds in the daemon's own environment and it sends them as `X-Registry-Auth` on pulls — a private image works without ever making the package public. Either `GHCR_USERNAME` + `GHCR_TOKEN` (a token with `read:packages`) for `ghcr.io`, or the general `REGISTRY_AUTHS` = a JSON map of `{"<registry-host>": {"username": "...", "password": "..."}}`. Creds live in the sealed CVM env, never in `project.json`. Public pulls are unchanged.

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

# Or push a new tarball to a tarball-deployed project
curl -X POST $CVM/_api/projects/my-app/redeploy -H "Authorization: Bearer $TOKEN" -F "files=@app.tgz"

# Tear down
curl -X DELETE $CVM/_api/projects/my-app -H "Authorization: Bearer $TOKEN"
```

## Create a project without the owner token (RFC 0034)

Deploy scripts should not carry the owner token. Mint one long-lived **create** token and
keep it in one place; it can only create projects that do not exist yet.

```bash
curl -X POST $CVM/_api/tokens -H "Authorization: Bearer $OWNER" \
  -d '{"scope":"create","ttl":31536000,"max_pending":5}' | jq -r .token > ~/.config/dstack-webhost/provisioner
```

Creating with it returns the project plus a per-project token (`projects/<name>` scope,
one year) in the `token` field. Save that beside the project and use it for every later
`redeploy`, `logs`, `promote`, `DELETE`. `examples/hello-pending/deploy.sh` is the whole flow.

The new project serves immediately but is **pending**: `approval: {status: "pending",
deadline, created_by}`. It cannot be promoted, and at the deadline (`DAEMON_PENDING_TTL`,
default 7 days) it is frozen: container stopped, files and volumes kept, `/<name>/`
answers 503 `pending expired`. The owner reviews and approves:

```bash
curl $CVM/_api/projects?pending=1 -H "Authorization: Bearer $OWNER"
curl -X POST $CVM/_api/projects/my-app/approve -H "Authorization: Bearer $OWNER"
```

A create token can hold at most `max_pending` unapproved projects (default 5); the next
create returns 429. Set `DAEMON_NOTIFY_HOOK` (an executable, or an `http…` URL) on the
daemon to receive a JSON envelope on `create`, `approve`, and `freeze`.

## API surface

Public (no auth required), only for **attested** projects:

| | |
|---|---|
| `GET /` | Listing of attested projects. `Accept: text/html` returns the daemon's viewer page; `Accept: application/json` returns JSON. |
| `GET /_api/substrate` | The substrate's runtime configuration: effective OCI runtime (e.g. `sysbox-runc`), the runtimes Docker actually has (live `GET /info`), network-isolation posture (`host`/`sandbox`/`netns`), supported isolation modes, deno entry-shim hash. Lets a relying party verify what's mediating tenant syscalls — and see a configured/available mismatch rather than trust a name. |
| `GET /_api/projects/<name>` | Project manifest. |
| `GET /_api/projects/<name>/audit` | Audit log. |
| `GET /_api/attest/<name>` | Raw dstack quote. |
| `GET /_api/verification/<name>` | Manifest + quote + audit, in one response. |

Authenticated (`Authorization: Bearer $TOKEN`):

| | |
|---|---|
| `GET /_api/projects` | All projects, including dev. |
| `POST /_api/projects` | Deploy. |
| `POST /_api/projects/<name>/promote` | Dev → attested. Refused (403) while pending. |
| `POST /_api/projects/<name>/redeploy` | Re-pull from source, or multipart `files` to push a tarball. |
| `POST /_api/projects/<name>/approve` | Owner only: clear pending, unfreeze. |
| `POST /_api/tokens` | Owner only: mint a scoped token (`projects/<name>`, or `create` with `max_pending`). |
| `DELETE /_api/projects/<name>` | Tear down. |

## Signing users in

If the pod runs the `oauth3` app, your project gets user sign-in without implementing any of it.
The oauth3 app serves its own client, and every project on a pod is path-routed under one origin,
so the import is same-origin and needs no build step:

```html
<script type="module">
  import { auth } from "/oauth3/sdk.js";
  const o3 = auth({ node: location.origin + "/oauth3" });

  const me = await o3.me();              // { signedIn, subject?, providers, links }
  if (!me.signedIn) await o3.signIn();    // -> the pod's login page, and back
</script>
```

`signIn()` hands off to the pod's own login page, so you inherit every door it has configured
(passkey, GitHub, Google, openkey, did:key) without writing any of them. `/oauth3/sdk-ui.js`
additionally exports `signInButton(el, {auth})` if you want a drop-in control.

**One sign-in covers the whole pod.** The session lives under the shared `oauth3_session` key, so
a user who signed in on any other project here arrives already signed in. The same property has a
sharp edge worth knowing: one origin means `localStorage` is shared, so a token your project keeps
there is readable by every other project on the pod. Do not treat it as private to you.

Two limits to plan around:

- **Sign-in is open to any project; data access is not.** Reading a user's data through an oauth3
  plugin (`connect()`) requires your app to be curated into the oauth3 app's listing first.
- **`/oauth3/sdk.js` is fetched at runtime**, so it is not covered by your project's measured tree
  hash. If your project is `attested`, vendor the SDK into your own tree instead.

## Shipping the daemon itself

Two paths roll a CVM to a new daemon image, both through `ship-fix.sh`. **CI-built (staging, no ghcr credential):** every push to `staging` makes [`.github/workflows/staging-image.yml`](.github/workflows/staging-image.yml) build the `Dockerfile` and push `ghcr.io/amiller/tee-socket-proxy:staging-<short sha>` with the workflow's own `GITHUB_TOKEN`; `ship-fix.sh staging --no-build` then resolves that tag's digest anonymously from ghcr (the package is public), pins it in the staging compose and `phala deploy`s — and refuses, changing nothing, if CI has not built an image for HEAD. **Local (any target):** plain `ship-fix.sh staging|prod|pod` builds and pushes the image itself, so it needs `docker login ghcr.io` on the shipping machine.

## Where to look in the daemon

`proxy/ingress.py` has the request routing and auth gate. `proxy/runtimes.py` has the language-runtime container management and the Deno router that loads your handler. `proxy/deploy.py` has the git-clone path and the source-hash recording. The whole thing is small enough to read end-to-end.

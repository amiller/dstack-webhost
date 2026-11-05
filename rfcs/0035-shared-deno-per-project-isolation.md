# RFC 0035: Per-project isolation inside the shared Deno runtime

Status: Draft (implemented in the PR that adds this file; not deployed)

## Problem

The shared Deno runtime is one container running one `deno run --allow-all` router that
`import()`s every project's handler. Each project has its own dstack proxy at
`/run/broker/<project>/dstack.sock` (`dstack_proxy.DstackProxyManager`). The proxy builds
the GetKey path from the project that owns the socket and ignores the caller's `path`, so the
socket *is* the identity: whoever can connect to `/run/broker/relay-broker/dstack.sock` gets
relay-broker's keys.

Before this change the shared runtime mounted the whole broker parent at `/run/broker`
(`_shared_broker_binds`, "co-trust by construction"). Every co-resident could open every
other project's socket. On Nov 5 2026 `relay-broker-layover` derived relay-broker's payments
key `0xd3DD…03E7` this way (souk-co/layover#49). Nobody had to try: relay-broker's
`key.ts` defaults the socket to `/run/broker/relay-broker/dstack.sock`, so a second
deployment of the same code reaches the first one's socket.

The socket was not the only thing shared. Inside that one process a co-resident could also:

- read `/projects/<other>/project.json`, including its `env` (plaintext secrets), and its files;
  in `DAEMON_VOLUME_NAME` mode the router mounts the whole daemon volume at `/daemon-vol`;
- read and write `/daemon-data/<other>`;
- reach into the other handlers' heap (`globalThis`, a patched `fetch`, module state);
- run any binary with `Deno.Command`.

And the node/python shared runtimes got the same whole-parent mount, so they could reach the
sockets of deno projects, and of `isolation: container` projects too. Moving a victim to
`isolation: container` did not protect it.

## What the shared runtime should guarantee

Required: a project can connect only to its own `/run/broker/<project>/` sockets, so it can
derive only its own keys and quotes.

Achievable with the same mechanism, so included: a project reads only its own files, reads
and writes only its own data dir, has its own V8 heap and process env, and cannot spawn
processes or load native code.

Not provided here:
- **Network.** Children share the container's network namespace and have `--allow-net`. A
  project can reach whatever the container can, including every co-resident's public HTTP
  surface through the router, which the internet can reach anyway. Per-project networks
  still require `isolation: container`.
- **Kernel enforcement.** All children run as the same uid in one mount namespace. The
  boundary is Deno's permission checker, not the kernel. A Deno permission bug is a
  cross-project bug.
- **Resource fairness.** One project can still starve the CPU or memory of the others.

## Options, most minimal first

**(a1) One Worker per project with `deno.permissions`.** Smallest memory cost, but on Deno
2.9.8 per-worker permissions still require `--unstable-worker-options` (checked: without the
flag, `new Worker(..., {deno: {permissions}})` throws "Unstable API"). The worker lives in the
`--allow-all` process, so the boundary is an unstable API inside one address space. The
router would still need to proxy requests into the worker.

**(a2) One `deno run` subprocess per project with scoped flags.** Uses the stable CLI flags,
and the same recipe `isolation: container` already runs in production (`start_isolated`).
Separate process, separate heap. Costs about 39 MB RSS per idle child (measured: a
hello-world child), plus whatever the app itself uses, which it used inside the shared process
before anyway. **Chosen.**

**(b) Per-project socket token.** The proxy would demand a token that only the owning
project holds. In the old shared runtime the token travels through `ctx.env` and the shared
heap, which co-residents can read, so it adds nothing there. Once (a) exists, it is redundant
for `dstack.sock`. (`creds.sock` already works this way; `BROKER_TOKEN` is not injected in the
shared runtime today.)

**(c) Per-project containers.** Already exists as `isolation: container`. It is heavier (one
container and one Docker network per app) and, without the bind change below, it did not
protect against the shared runtime anyway.

## Design

The router stays trusted daemon code with `--allow-all`, but never imports app code. For each
project it:

1. resolves `files = realpath(/projects/<name>/files)` and `data = realpath(/daemon-data/<name>)`;
2. walks both and refuses to start the project if any symlink is dangling or resolves outside
   `files`/`data` (see below);
3. spawns

   ```
   deno run --no-prompt --no-check
     --allow-read=<files>,<data>,/run/broker/<name>,/tmp/tee-rt/<name>
     --allow-write=<data>,/run/broker/<name>,/tmp/tee-rt/<name>
     --allow-net --allow-import --allow-env --allow-sys --deny-run --deny-ffi
     /tmp/tee-rt/_child.ts <entry-url> <data> <env-json> /tmp/tee-rt/<name>/http.sock
   ```

   The child shim imports the handler, runs the existing `/_warmup` call, and serves
   `handler(req, {env, dataDir})` on its unix socket;
4. proxies `/<name>/...` to that socket with `fetch` and a unix `createHttpClient`, keeping the
   original origin, so `req.url` in the handler is what it was before.

The handler contract, `ctx.env`, `ctx.dataDir`, warmup, the 404 project list, logs on the
container's stdout, and the SIGTERM drain are unchanged. `proxy/test_deno_drain.py` passes
against the new router without edits beyond the image constant.

Facts the design relies on, checked on `denoland/deno:2.9.8`:

- A unix-socket connect needs **both** read and write permission on the socket path. This
  holds for `Deno.connect({transport: "unix"})` and for `fetch` through
  `createHttpClient({proxy: {transport: "unix"}})`, the path relay-broker uses. Missing
  either gives `NotCapable`. `/run/broker` is mounted read-only; that does not matter to
  connect(2).
- `--deny-*` overrides `--allow-*`, including `-A`.
- **Permission checks use the path as written and do not resolve symlinks.** A symlink at
  `<allowed>/x -> /run/broker/b/dstack.sock` connected and returned b's key; a symlink to a
  directory exposed its files. A child cannot create symlinks (`Deno.symlink` requires
  unscoped read and write). Symlinks can still arrive two ways: `git clone` keeps them in
  `files` (tarball deploys already reject them with `filter="data"`), and any app that ran
  under `--allow-all` before this change could have planted one in any `/daemon-data/<x>`.
  Hence the router-side walk in step 2. It fails closed; a project with an escaping or
  dangling link does not start and the router logs which path.

Flag choices that differ from `isolation: container`:

- `--allow-env` instead of `--deny-env`. The shared container is created with no env, so a
  child's process env is just the image's (`PATH`, `DENO_DIR`, ...), and each child has its own.
  Under `--deny-env`, `Deno.env.get` throws, and live apps call it at module load
  (`meeting-brainrot`, dsmack `payments/configure.mjs`). Project secrets stay in `ctx.env`.
- `--allow-sys`: host facts (hostname, load, interfaces), nothing from another project.
- `--allow-import`: remote imports are not a cross-project channel, and Deno 2's default
  import allowlist would otherwise break apps importing from hosts outside it.
- `--deny-run` stays: a spawned process is outside the Deno sandbox. An allowlist like
  `--allow-run=git` would not help, since git reads arbitrary paths and runs configured
  commands.

Broker mounts: `_shared_broker_binds(projects)` now binds one subdir per co-resident project,
`<BROKER_HOST_PATH>/<name>:/run/broker/<name>:ro`, instead of the parent. The path apps use
does not change, and projects in other containers (node/python runtimes, `isolation:
container`, image apps) are no longer visible in this one. Node and python shared runtimes
are still co-trust among their own residents; this RFC does not change them.

The Deno image is pinned to `denoland/deno:2.9.8` (it was `:latest`, re-pulled on every
refresh; today's `:latest` has the same digest). The boundary now rests on Deno's
permission semantics, so a Deno upgrade should be a reviewed change with
`proxy/test_deno_isolation.py` re-run, not a silent re-pull.

## What changes for apps

- `Deno.Command`, `Deno.dlopen`, and any read or write outside the project's own files, data
  dir and broker dir now throw `NotCapable`. Survey of the app sources on the laptop (dsmack
  `applications/*`, `webhost-apps/*`): no shared-runtime handler path uses `Deno.Command` or
  FFI (`private-agent-web/directory-health.mjs` uses it in a CLI path). No app uses
  `upgradeWebSocket`, which did not work through the old router either.
- An app that reads its own `project.json` (`/projects/<name>/project.json`) or other paths
  under `/projects/<name>/` outside `files/` will now fail. None found.
- An app that relied on reaching another project's socket breaks. That is the point.
  `relay-broker-layover` already moved to its own socket (amiller/dsmack#89, signer
  `0x8674…7632`).
- A child that exits (for example an unhandled rejection) now takes down only its own
  project; the router drops it from routing and logs the exit code. It comes back on the next
  refresh (any deploy to that runtime, or a daemon restart). Before, the same crash took down
  the whole runtime. No respawn loop is added.
- Memory: one Deno process per project. Measured locally: router 52 MB, idle child 39 MB,
  relay-broker child 225 MB.

## Residual risks

- `<env-json>` is on the child's argv, as in `isolation: container`. Siblings would need read
  access to `/proc` to see it, which they do not have.
- `DENO_DIR` (module cache) is shared. Only Deno itself writes it; children have no fs
  permission on it. A child can only cause URLs it names to be cached.
- The symlink walk runs at every runtime refresh and costs one `lstat` per file in each
  project's files and data dirs.

## Tests

`proxy/test_deno_isolation.py` runs the real router in `denoland/deno:2.9.8` with three
projects, and real `DstackProxyManager` sockets in front of a fake dstack that returns
`sha256(path)`. Project a gets its own key; a's attempts on b's socket, b's
`project.json`, b's data, b's code (`import()`), b's router socket, and `Deno.Command`
all fail with `NotCapable`; a's `ctx.env`, `ctx.dataDir`, `req.url`, POST bodies and gzip
pass through; b still derives its own key and was warmed up; project c, whose files contain
`leak -> /run/broker/b/dstack.sock`, is refused while a and b serve.

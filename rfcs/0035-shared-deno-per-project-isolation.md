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
  project can reach whatever the container can, including every co-resident's HTTP surface
  through the router at `localhost:3000/<name>/`, bypassing the ingress. Today the ingress
  injects no auth-derived headers (it passes client `X-Forwarded-*` through), so this is
  close to what the internet sees, but it is pre-existing and unchanged here. Per-project
  networks still require `isolation: container`.
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
   `files`/`data`, or any file in `data` has more than one hard link (see below);
3. spawns

   ```
   deno run --no-prompt --no-check
     --allow-read=<files>,<data>,/run/broker/<name>,/tmp/tee-rt/<name>
     --allow-write=<data>,/run/broker/<name>,/tmp/tee-rt/<name>
     --allow-net --allow-import --allow-env --allow-sys --deny-run --deny-ffi
     /tmp/tee-rt/_child.ts <entry-url> <data> /tmp/tee-rt/<name>/http.sock   (env JSON on stdin)
   ```

   The child shim reads `env` from stdin, imports the handler, runs the existing `/_warmup`
   call, and serves `handler(req, {env, dataDir})` on its unix socket. A child that is not
   listening within 120 s is killed. A child that exits for any reason is respawned with
   backoff (1 s doubling to 60 s, reset once a run lasts a minute); while it is down its
   path answers 503, and the other apps keep serving;
4. proxies `/<name>/...` to that socket with `fetch` and a unix `createHttpClient`, keeping the
   original origin, so `req.url` in the handler is what it was before.

**Invariant: `_child.ts` must stay the child's main module, and the entry specifier must come
from `Deno.args`.** Deno loads a dynamic import whose specifier is a string literal in the
main module as part of the module graph, without a permission check, and once loaded any
module in the process can import it again. Checked on 2.9.8: with
`--allow-read=/x/a`, a literal `import("file:///x/b/s.ts")` in the main module succeeded, and
the same literal inside the app (imported via `Deno.args`) then succeeded too; with no
literal in the main module, the app's import failed. The shim imports nothing by literal.

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
  The same apps could have hard-linked another project's data file into their own data dir
  (`/daemon-data` is one filesystem). Hence the router-side walk in step 2. It fails closed:
  a project with an escaping or dangling symlink, or a data file with `nlink > 1`, does not start
  and the router logs the path. Both ends of a planted hard link are refused, since the
  router cannot tell which side planted it. This does not undo any tampering with another
  project's data done before the change. Hard links are not checked in `files/`: the daemon
  writes it, and a clone from a local path hard-links `.git/objects`.

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
  project, which answers 503 until the router respawns it. Before, the same crash took down
  the whole runtime. A child that hangs while loading no longer holds up the router; it is
  killed at the startup timeout and retried with backoff.
- Memory: one Deno process per project. Measured locally: router 52 MB, idle child 39 MB,
  relay-broker child 225 MB.

## Residual risks

- `ctx.env` reaches the child on stdin, so it is not on any argv and `ps` does not print it.
  (`isolation: container` still passes it on argv.)
- `DENO_DIR` (module cache) is shared. Only Deno itself writes it; children have no fs
  permission on it. A child can only cause URLs it names to be cached.
- The link walk runs at every runtime refresh and costs one `lstat` per file in each
  project's files and data dirs.

## Tests

`proxy/test_deno_isolation.py` runs the real router in `denoland/deno:2.9.8` with three
projects, and real `DstackProxyManager` sockets in front of a fake dstack that returns
`sha256(path)`. Project a gets its own key; a's attempts on b's socket, b's
`project.json`, b's data, b's code (`import()`), b's router socket, and `Deno.Command`
all fail with `NotCapable`; a's `ctx.env`, `ctx.dataDir`, `req.url`, POST bodies and gzip
pass through; b still derives its own key and was warmed up; project c, whose files contain
`leak -> /run/broker/b/dstack.sock`, and project d, whose data dir holds a hard link into
another data dir, are refused while a and b serve. Project h, whose module never finishes
loading, is killed at the (test-shortened) startup timeout and answers 503 while the others
serve. No `/proc/*/cmdline` in the container contains a's env value. After a calls
`Deno.exit(1)` it answers 503, then is respawned and serves the same `ctx.env`. Against the old router the same test
fails at the first cross-project check: a gets b's key.

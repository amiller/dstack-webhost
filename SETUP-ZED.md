# SETUP-ZED.md — tee-daemon dev setup on the `zed` machine

This documents the **coding-only** local setup of tee-daemon (the public `dstack-webhost`
repo) on the `zed` box. No CVM/deploy credentials are involved — see
[No credentials / what a deploy would need](#no-credentials--what-a-deploy-would-need).

## One-branch-per-machine convention

Each machine gets its own git branch; you commit locally and the other machine *pulls*.

- **zed** works on branch **`zed-work`**.
- The **laptop** works on **`feat/mount-broker-attested-apps`** and owns deploys.
- **Never force-push, never touch another machine's branch.** Push only when explicitly asked.

## Machine facts

- No `sudo`. Docker works (rootful daemon reachable at `/var/run/docker.sock`).
- System interpreters: `python3` = **3.8.10**, `node` = **22.23.1**.
- `bun` and `deno` were missing (tee-daemon needs both); installed home-dir, below.

## Tool versions (as installed)

| Tool | Version | Location |
|------|---------|----------|
| bun  | 1.3.14  | `~/.bun/bin/bun` |
| deno | 2.8.3   | `~/.deno/bin/deno` |
| uv   | 0.11.24 | `~/.local/bin/uv` |
| python (venv) | 3.11.15 | `.venv/` (provisioned by uv) |
| docker server | 28.1.1 | system |
| node | 22.23.1 | system |

## PATH for a working session

```sh
export PATH="$HOME/.local/bin:$HOME/.deno/bin:$HOME/.bun/bin:$PATH"
```

(The bun installer already appended `~/.bun/bin` to `~/.bashrc`; the others are added here
explicitly so the daemon's runtime tooling is on PATH.)

## Steps that were run

### 1. Install bun (no sudo)
```sh
curl -fsSL https://bun.sh/install | bash
export PATH="$HOME/.bun/bin:$PATH"
bun --version   # 1.3.14
```

### 2. Install deno (no sudo)
```sh
curl -fsSL https://deno.land/install.sh | sh
export PATH="$HOME/.deno/bin:$PATH"
deno --version  # 2.8.3
```

### 3. JS deps with bun
`package.json` is **gitignored** (see `.gitignore`), and `bun.lock` is gitignored too — it is a
local-only artifact (present in this working tree, not committed) that carries the workspace
dependencies embedded. So `package.json` has to be recreated from that local lockfile before
`bun install`. The four workspace deps (taken verbatim from `bun.lock`'s
`workspaces[""].dependencies`):

```json
{
  "name": "tee-daemon",
  "private": true,
  "type": "module",
  "dependencies": {
    "@ai-sdk/openai": "^3.0.50",
    "ai": "^6.0.146",
    "smithers-orchestrator": "^0.14.1",
    "zod": "^4.3.6"
  }
}
```
```sh
bun install    # 616 packages; bun.lock unchanged (recreated package.json matched exactly)
```
> This `package.json` is local-only (gitignored). Recreate it as above on a fresh checkout.

### 4. Python side (the aiohttp daemon under `proxy/`)
**Gotcha — the system python is too old.** The `proxy/` code uses PEP 585/604 annotation
syntax (`dict[str, ...]`, `str | None`) *without* `from __future__ import annotations`, so it
requires **Python ≥ 3.10**. The Dockerfile confirms the intended runtime: `FROM python:3.11-slim`.
On the system `python3.8`, `python -m proxy.main` dies immediately with
`TypeError: 'type' object is not subscriptable`.

**Workaround (no sudo):** install `uv` and let it provision Python 3.11 (matches the Dockerfile),
then build the venv on that.
```sh
curl -fsSL https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python aiohttp requests playwright
.venv/bin/playwright install chromium     # browser binaries -> ~/.cache/ms-playwright
```
There is **no requirements file**; deps were derived from imports:
- `aiohttp` — the daemon proxy itself (`proxy/main.py`, `ingress.py`, …). `proxy/secp.py` is
  pure-Python secp256k1 with no external deps.
- `requests` + `playwright` — used only by `test_daemon.py` (end-to-end browse test).

Installed: `aiohttp==3.14.1`, `requests==2.34.2`, `playwright==1.60.0`.

> `playwright install-deps` (system libs) can't run without sudo. Headless chromium worked
> for the test anyway; if you ever hit a missing-shared-lib error, that's the cause.

### 5. Run the test suite
```sh
export PATH="$HOME/.deno/bin:$HOME/.bun/bin:$PATH"
export BROKER_SOCKET_DIR="$(mktemp -d /tmp/tee-broker.XXXXXX)"   # see gotcha below
.venv/bin/python test_daemon.py
```

**Gotcha — `BROKER_SOCKET_DIR`.** `test_daemon.py` predates the broker feature; its
`start_daemon()` does not set `BROKER_SOCKET_DIR`, which defaults to `/var/run/broker`
(root-only → `PermissionError`). The test builds its env as `{**os.environ, ...}`, so exporting
`BROKER_SOCKET_DIR=<writable dir>` in your shell passes through without editing the test.

The test needs docker (it spins up real containers). It pulls these images on first run:
`denoland/deno:latest`, `node:22-slim`, `python:3.12-slim`, `nginx:alpine`. Pre-pull them to
avoid a slow first run:
```sh
for img in denoland/deno:latest node:22-slim python:3.12-slim nginx:alpine; do docker pull "$img"; done
```

#### Result (verbatim, on a clean run)
**22 tests pass, 1 fails, 3 do not run** (the suite aborts on first failure). All passing:
```
--- Test: API auth ---                              Auth: 401/403/200/200 ✓
--- Test: deploy static from git ---                ✓
--- Test: static serving ---                        Content verified ✓
--- Test: .git path blocked ---                     .git blocked ✓
--- Test: Playwright static ---                     Playwright verified ✓
--- Test: deploy deno from git with project.json    ✓
--- Test: deno handler ---                          ✓
--- Test: container runtime selection ---           Runtime=runc ✓
--- Test: deploy image-runtime project (nginx) ---  ✓
--- Test: image-runtime ingress (nginx serves /) -- nginx served 896 bytes ✓
--- Test: image-runtime adopts existing volume ---  Volume survived teardown ✓
--- Test: two deno projects isolation=container --- A cannot read B's files ✓
--- Test: image-runtime tenants separate networks - a -> b blocked ✓
    (verbatim log: "tenants" = image-runtime apps)
--- Test: isolation:container per-project volume -- per-project volume created ✓
--- Test: image-runtime env_passthrough ---         container correctly missing it ✓
--- Test: image-runtime redeploy preserves manifest Redeploy preserved ✓
--- Test: public /_api/substrate runtime identity - effective_runtime=runc ✓
--- Test: auto-detect runtime from files ---        Verified: {'detected': 'python'} ✓
--- Test: deploy static via multipart tarball ---   Content served ✓
--- Test: multipart missing 'files' field ---       400 ✓
--- Test: multipart missing 'manifest' field ---    400 ✓
--- Test: multipart malformed manifest JSON ---     400 ✓
```
The one failure:
```
--- Test: redeploy after git push ---
Traceback (most recent call last):
  File "test_daemon.py", line 731, in <module>
    main()
  File "test_daemon.py", line 718, in main
    test_redeploy()
  File "test_daemon.py", line 310, in test_redeploy
    assert result["commit_sha"] != old["commit_sha"]
                                   ~~~^^^^^^^^^^^^^^
KeyError: 'commit_sha'
```
Not reached afterward: `test_audit_log`, `test_list_projects`, `test_teardown`.

#### Root cause of the failure (pre-existing daemon bug, NOT an environment problem)
`test_redeploy` starts with `old = api_get("/projects/test-static").json()`. That authenticated
**GET `/_api/projects/<name>` returns 404 for any non-attested (dev-mode) project**, so `old`
is `{"error": "not found"}` and `old["commit_sha"]` raises `KeyError`. Reproduced in isolation
(deploy → 201, `project.json` on disk, GET → 404), so it is independent of the zed setup.

In `proxy/ingress.py` `_handle_api`, the RFC 0015 public-verifier branch runs first for every
GET:
```py
if method == "GET":
    public_name = self._public_attested_path(path)   # matches "projects/<name>"
    if public_name is not None:
        project = self.store.load(public_name)
        if project.mode != "attested":
            return 404                                # <-- shadows the authed GET below
```
`_public_attested_path("projects/<name>")` returns `<name>`, so the request short-circuits to
404 before ever reaching the authenticated `_api_status` dispatch — even with a valid admin
Bearer token. `test_redeploy` is the only test that does an authenticated single-project GET,
which is why it's the only one that trips. **Left unpatched** (daemon code is laptop-owned WIP);
flagged here for the owner. A fix would let an authenticated admin fall through instead of 404,
and only short-circuit unauthenticated callers.

## How to run the daemon manually on zed

```sh
export PATH="$HOME/.local/bin:$HOME/.deno/bin:$HOME/.bun/bin:$PATH"
T=$(mktemp -d /tmp/tee-daemon.XXXXXX)
INGRESS_PORT=8080 \
DAEMON_DATA_DIR=$T/projects DAEMON_AUDIT_DIR=$T/audit DAEMON_TUNNEL_DIR=$T/tunnels \
PROXY_SOCKET_DIR=$T/proxy BROKER_SOCKET_DIR=$T/broker \
DOCKER_SOCKET=/var/run/docker.sock DSTACK_SOCKET=/nonexistent \
TEE_DAEMON_TOKEN=dev-token \
  .venv/bin/python -m proxy.main
# API: http://localhost:8080/_api/...  (Authorization: Bearer dev-token)
```
`TEE_DAEMON_TOKEN` here is a **local dev token you make up** — it is the API auth token for the
local daemon, not a Phala/CVM credential. The test suite uses its own
`TEST_TOKEN = "test-secret-token-12345"`.

## No credentials / what a deploy would need

This setup obtained **zero** deploy/CVM secrets, by design. There is intentionally no `deploy/`
directory on zed (it is `.gitignore`d). A real *deploy* to the CVM (which stays on the laptop)
would need things this machine deliberately does **not** have:
- `TEE_DAEMON_TOKEN` for the **remote** daemon (the real CVM admin token, not the local dev one).
- A Phala/dstack **deploy key** / auth for the CVM and its dstack socket (`DSTACK_SOCKET`).

None of these are required for coding or for `test_daemon.py`, which runs entirely against a
local daemon + local docker.

## Repo hygiene notes

- `.venv/` was added to `.gitignore` (it is not committed).
- `package.json`, `node_modules/`, `tsconfig.json`, **and `bun.lock`** are all gitignored by
  design, so none of the JS setup travels via git. `bun.lock` (kept locally) is the source of
  truth for the JS dep versions; recreate `package.json` from it as shown in step 3.
- Playwright browser binaries live in `~/.cache/ms-playwright/` (outside the repo).

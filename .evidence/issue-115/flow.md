# Issue #115 — runsc not registered on pod.dstack.soc1024.com — status evidence (2026-08-29)

Verdict up front: **the fix has landed and is live on the pod; the remaining acceptance
items are pod-side, token/shell-gated operator steps.** This file records (1) today's
Tier-1 transcript against the live pod, (2) the fix lineage in git, and (3) a local
Tier-1 analog (real docker + the pinned runsc registered per `prelaunch.sh`) exercising
the daemon code paths at the exact commit the pod runs. Run on the `swarm` box — no CVM
credentials, no deploy token (per SETUP-ZED.md/AGENTS.md those live on the operator's
laptop), rootless docker only.

## 1. Live pod state (Tier 1, 2026-08-29, curl from swarm)

```
$ curl -s https://pod.dstack.soc1024.com/_api/version
{"version": "dev", "commit": "c7270819"}

$ curl -s https://pod.dstack.soc1024.com/_api/substrate
{
  "container_runtime": "runsc-hostnet",
  "effective_runtime": "runsc-hostnet",
  "available_runtimes": ["io.containerd.runc.v2", "runc", "runsc", "runsc-hostnet", "sysbox-runc"],
  "network_isolation": "host",
  ...
}
```

`available_runtimes` is read live from Docker `/info` by the post-#117 `_substrate_info()`
(`proxy/ingress.py`), so this is Docker on the pod **actually listing `runsc`** — the exact
state the issue said was missing ("unknown or invalid runtime name: runsc"). The daemon is
running with `DAEMON_CONTAINER_RUNTIME=runsc-hostnet` and passed `verify_configured_runtime()`
at boot, which only succeeds if Docker really has that runtime.

Current image tenants (public RFC-0015 `GET /_api/projects/<name>`, attested):

```
browser-spi    oci_runtime: runc   image ghcr.io/amiller/login-with-anything-browser@sha256:4005…
egress-vpn     oci_runtime: runc   image ghcr.io/amiller/openvpn-socks5@sha256:287c…
twitter-debug  oci_runtime: runc   image ghcr.io/amiller/tiktok-dstack:twitter-debug
```

(The Aug-18 tenant set — capdemo, caps-server, tinycloud, caps-probe, arena-daily — is gone;
the pod's project state was reset by the redeploy: per-project `audit` and `history` now
return empty. 13 projects total, 3 image-runtime.)

## 2. Fix lineage

- **#116** (the substrate half of this issue) — closed by **PR #117**
  (`dcaad9c9`): `/_api/substrate` reports Docker's real runtimes;
  `verify_configured_runtime()` (proxy/runtimes.py) refuses to start the daemon when
  `DAEMON_CONTAINER_RUNTIME` names a runtime Docker doesn't have. The silent
  false-advertisement hole is closed at the code level.
- **`51f63b91`** (2026-08-25, main) "ship-fix: the pod target must always carry the
  prelaunch" — `ship-fix.sh` pod target now always passes
  `--pre-launch-script $HA/deploy-notes/prelaunch-runsc-resilient.sh` and **refuses to
  deploy** if the script is missing, so a pod can no longer boot without runsc while
  intending it.
- The pod was redeployed and now serves `/_api/version` → `c7270819` (2026-08-27, main),
  which contains all of the above.

## 3. Local Tier-1 analog (swarm box, daemon at the pod's commit `c7270819`)

Environment: rootless docker (user systemd unit, `~/.config/docker/daemon.json`), Python
3.11 venv per SETUP-ZED.md. runsc installed exactly per
`examples/runsc-prelaunch/prelaunch.sh` values:

```
RUNSC_URL=https://storage.googleapis.com/gvisor/releases/release/20260622/x86_64/runsc
sha512 verified == 6df95d09…f89a6de4e   (pinned, dated, immutable release — the #32/#34 lesson)
registration: runsc, runsc-hostnet(--network=host)   [prelaunch.sh@c7270819 registers these two]
$ docker info | grep Runtimes:
 Runtimes: io.containerd.runc.v2 runc runsc runsc-hostnet
```

### 3a. Unbacked configured runtime now refuses to boot (was: silent lie)

```
$ DAEMON_CONTAINER_RUNTIME=runsc python -m proxy.main        # docker has no runsc yet
RuntimeError: DAEMON_CONTAINER_RUNTIME='runsc' is not registered with Docker
(available: io.containerd.runc.v2, runc); refusing to start
```

This is the guard that makes the #115 class of failure loud instead of silent.

### 3b. Substrate parity with the live pod

With `DAEMON_CONTAINER_RUNTIME=runsc-hostnet`, local `/_api/substrate` returns the same
shape/values as the pod's (§1) — `container_runtime: runsc-hostnet`,
`available_runtimes` incl. `runsc`.

### 3c. Criterion 2 path: image tenant with NO oci_runtime override

```
$ POST /_api/projects {"name":"probe115-default","runtime":"image",
                        "image":"nginx:alpine","image_port":80,"mode":"dev"}
HTTP 500 {"error": "start failed (400)"}

$ docker inspect tee-image-probe115-default-dev --format '{{.HostConfig.Runtime}} state={{.State.Status}}'
runsc-hostnet state=created
```

The container **was created** — `HostConfig.Runtime=runsc-hostnet`, i.e. the default path
resolved the runtime name. The original failure signature
(`create_container failed (400): unknown or invalid runtime name: runsc`) is gone; create
succeeds. The subsequent `start` failure is this box's rootless limitation (gVisor needs
cgroup/devices delegation rootless docker here doesn't have; `--rootless=true` is rejected
by runsc 20260622 with `Rootless mode not supported with "create"`), not a runtime-name
problem, and the daemon propagates it as a loud 500 — no fallback, no downgrade.

### 3d. Criterion 3 path: explicit oci_runtime override unaffected

```
$ POST /_api/projects {… "oci_runtime":"runc"}
HTTP 201;  docker inspect → HostConfig.Runtime=runc state=running
$ docker exec tee-image-probe115-ruc-dev wget -qO- http://127.0.0.1:80
<!DOCTYPE html><html><head><title>Welcome to nginx!</title>…
```

(Host-side ingress to the container IP 500s on this box only because rootless-docker
bridge IPs aren't reachable from host processes — an environment artifact, not runtime
selection; the exec-level check shows the tenant serving.)

## 4. Acceptance criteria mapping

| # | Criterion | Status |
|---|---|---|
| 1a | pod deployment uses the pinned-release prelaunch | Landed: `51f63b91` wires the prelaunch into every pod deploy (refuses without it); pinned 20260622 + sha512 verified here. **Letter-vs-deployed nuance:** ship-fix.sh points at `$HA/deploy-notes/prelaunch-runsc-resilient.sh` (private hermes-agent), not `examples/runsc-prelaunch/prelaunch.sh`; the registered-runtime set observed live (`runsc`, `runsc-hostnet`) matches `examples/…/prelaunch.sh@c7270819` exactly |
| 1b | host check: `/dstack/persistent/bin/runsc` executable; `docker info` lists runsc | docker-info half VERIFIED live via `/_api/substrate` (reads Docker `/info`); binary-path half needs shell on the CVM — SKIP (no host access from swarm) |
| 2 | no-override image tenant deploys on pod; container reports runsc | Code path VERIFIED locally (§3c: create succeeds, `HostConfig.Runtime=runsc-hostnet`); pod-side demo needs the admin token (POST → 401 here) — SKIP, runbook §5 |
| 3 | explicit-`runc` tenants keep working | Live tenants still pin `runc` (§1); local end-to-end VERIFIED (§3d); on-pod container liveness needs auth — SKIP |

## 5. Operator runbook for the remaining pod-side Tier-1 (token + shell)

On the operator's laptop (has `phala` creds, `PRIVATE_KEY`, pod `TEE_DAEMON_TOKEN`):

```sh
# (0) host checks on the CVM (criterion 1b)
phala cvms ssh oauth3-prod7   # or console
  ls -l /dstack/persistent/bin/runsc && /dstack/persistent/bin/runsc --version
  docker info | grep -i runtimes        # expect runsc + runsc-hostnet listed
  docker run --rm --runtime=runsc alpine uname -a   # expect gVisor's "4.4.0" signature

# (1) criterion 2: default-runtime image tenant (NO oci_runtime in manifest)
curl -sS https://pod.dstack.soc1024.com/_api/projects \
  -H "Authorization: Bearer $POD_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"probe115","runtime":"image","image":"nginx:alpine","image_port":80,"mode":"dev"}'
#   expect HTTP 201 (NOT 500 "unknown or invalid runtime name: runsc")
docker inspect tee-image-probe115-dev --format '{{.HostConfig.Runtime}}'   # runsc-hostnet
#   (this deploy also serves via ingress: GET https://pod…/_api is authed; route /probe115/)

# (2) criterion 3: one existing runc tenant redeploy + running
curl -sS -X POST https://pod.dstack.soc1024.com/_api/projects/browser-spi/redeploy \
  -H "Authorization: Bearer $POD_TOKEN"
docker inspect tee-image-browser-spi-attested --format '{{.HostConfig.Runtime}} {{.State.Status}}'

# (3) cleanup
curl -sS -X DELETE https://pod.dstack.soc1024.com/_api/projects/probe115 \
  -H "Authorization: Bearer $POD_TOKEN"
```

## 6. Observations (not fixed here — flagged for the owner)

- `start` errors surface as `{"error": "start failed (400)"}` without the OCI message,
  while `create` errors include it (`create_container failed (400): {'message': …}` —
  the string quoted in the issue). Same root as the issue's "Also": diagnosis cost.
- The issue's "Also" (deploy scripts using `curl -fsS` discard the error body) is
  webhost-apps-side and untracked by any issue AFAICT.
- Local analog ran at the pod's exact commit `c7270819` via a detached git worktree; the
  swarm box's docker config was restored to as-found afterwards (runtimes back to
  `io.containerd.runc.v2, runc`; no leftover containers/volumes/networks).

## 7. Follow-up 2026-08-29 16:10 UTC (second spawn): prelaunch made loud — branch `staging-115`

Live pod re-verified before touching anything (unchanged from §1): `/_api/version` →
`c7270819`; `/_api/substrate` → `available_runtimes` incl. `runsc`, `runsc-hostnet`;
`container_runtime: runsc-hostnet`.

The in-repo remainder of this issue was the tail of `examples/runsc-prelaunch/prelaunch.sh`:
both the docker restart and the registration "check" ended in `|| true` (and `2>/dev/null`
hid Docker's own error) — the exact silent mechanism this issue was filed over ("Likely
mechanism"; AGENTS.md no-fallbacks). On `staging-115`:

- `systemctl restart docker || service docker restart` — a failure of both methods now
  aborts the script (was `|| true`).
- The post-restart check now asserts `runsc`, `runsc-hostuds`, `runsc-hostnet` appear in
  Docker's `Runtimes:` line and exits 1 naming the missing runtime (was
  `docker info 2>/dev/null | grep -iE "runtime" || true`, which cannot fail). This is
  criterion 1's "post-boot host check" made part of the provision itself.

`effective_runtime` (still `rt or "runc"`) was deliberately NOT changed: the #117 boot
guard makes the echo sound — the daemon cannot serve a configured runtime Docker lacks —
and post-boot drift stays visible via the live `available_runtimes` pair, exactly the
configured/available visibility the issue asked for. Changing it to an intersection
would hide the configured value, which is the more useful fact when diagnosing drift.

Stub matrix — the full script run in a throwaway `alpine` container (`--rm`), real
script with only `RUNSC_SHA512` patched to the stub payload's hash; stub
`curl`/`systemctl`/`service`/`docker` on PATH; host docker config untouched:

| scenario | script at staging tip | `staging-115` |
|---|---|---|
| all three runsc runtimes registered | `done`, exit 0 | `done`, exit 0 |
| `runsc` missing from Docker | **`done`, exit 0 (silent)** | `runtime 'runsc' not registered with Docker after restart`, exit 1 |
| docker daemon broken post-restart | `done`, exit 0 (stderr hidden) | docker's own stderr, abort, exit 1 |
| both restart methods fail | `done`, exit 0 | abort at the restart line, exit 1 |

`test_daemon.py` is not extended: it exercises the daemon over real docker, not CVM
host provisioning; `bash -n` plus the matrix above is the verification for this change.
The overseer suite still gates the branch (nothing in it touches prelaunch).

## 8. Delivery status (same spawn)

- Branch `staging-115` pushed via the git broker. No PR opened from here: the gh broker
  refuses `amiller/dstack-webhost` ("not in the swarm set") for every verb (`issue`,
  `pr`, `label`), so the mandated `ready` → `needs-triage` swap + evidence comment are
  operator steps; paste-ready text in the swarm home (`~/issue115/issue-115-comment.md`).
- Pod-side acceptance (§5) remains token/shell-gated, unchanged.

## 9. Third spawn (2026-08-29 ~18:00 UTC): matrix re-verified first-hand; suite + gh blockers pinned

Independently re-ran the §7 stub matrix from scratch (fresh stub toolchain, `bash:latest`
container, `RUNSC_SHA512` patched to the stub payload's real hash so the integrity gate
actually runs; `bash -n` clean on both versions). Same result table:

| scenario | staging tip | `staging-115` |
|---|---|---|
| all three runsc runtimes registered | `done`, exit 0 | `done`, exit 0 |
| `runsc` missing from Docker | **`done`, exit 0 (silent)** | `runtime 'runsc' not registered with Docker after restart (Runtimes:…)`, exit 1 |
| docker daemon broken post-restart | **`done`, exit 0, stderr discarded** | docker's own stderr, exit 1 |
| both restart methods fail | **`done`, exit 0** | aborts at the restart line, exit 1 |

Merge-gate suite attempted, cannot run as `swarm`: `test_daemon.py` hard-codes
`DOCKER_SOCKET=/var/run/docker.sock` in the daemon env (test_daemon.py:115) and
`/var/run` is root-owned, while this account's docker is rootless
(`/run/user/1018/docker.sock`; `docker context ls` → `rootless *`). The daemon dies in
`ensure_network()` with `UnixClientConnectorError … /var/run/docker.sock … Permission
denied` and the suite exits `RuntimeError: Daemon failed to start`. Dependencies were not
the blocker (3.11 venv with aiohttp/requests/playwright + headless chromium built at
`/tmp/suite-venv`; the four suite images pre-pulled). The suite references `prelaunch.sh`
nowhere (grep: 0 hits), so for this branch it is a regression gate only — the overseer
run (root docker) still applies.

GitHub side, pinned with exact commands:

```
$ gh issue view 115 -R amiller/dstack-webhost
repo 'amiller/dstack-webhost' not in the swarm set
$ gh api repos/amiller/dstack-webhost/compare/staging...dcaad9c9 --jq .ahead_by
repo 'dstack-webhost' not in the swarm set
```

while `git push` through the broker succeeds — its repo key is the clone directory
basename (`tee-daemon`), which the grant matches; gh derives the slug from the git remote
(`amiller/dstack-webhost`), which it does not. The gate is keyed on a name the GitHub repo
does not have (the git_remotes.md lesson, in the gate itself). Local-git equivalent of the
containment proof, since the gh form is unreachable: `git merge-base --is-ancestor
dcaad9c9 origin/staging` → yes; `c7270819` (what the pod serves, re-verified live this
spawn: `/_api/version` → `c7270819`, `/_api/substrate` → `available_runtimes` incl.
`runsc`, `runsc-hostnet`) is on `origin/main`, not staging.

Consequence: PR create, the label swap, and the evidence comment are operator steps until
the swarm set carries `dstack-webhost`. `ready` is still on the issue with no open PR, so
the router re-spawns it every tick — three spawns on this issue today, all after `ready`
was applied at 14:00 UTC, two of them after the branch was already pushed. §8's
"Relabeled `needs-triage`" was aspirational, not performed; the paste-ready text at
`~/issue115/issue-115-comment.md` has been corrected accordingly, and a copy of the
operator handoff dropped in `/srv/swarm-outbox/`.

## 10. Delivery (fifth worker spawn, 2026-08-29, later same day)

The repo-set fix landed (both `tee-daemon` and `dstack-webhost` listed — the LESSONS entry
from this same morning), and brokered gh reached this repo again. Granted verbs, exercised
first-hand: `issue view/list`, `pr list`, `api …/events` (reads, already used above), and
`pr create` / `issue edit` / `issue comment` with the body passed as an inline `--body`
string. Refused: any file-reading flag (`--body-file`, even `-`), `auth`, bare `api user`.

Delivered, in order, from the pushed branch unchanged (`01376b60` at PR-open):

1. **PR #134** opened — base `staging`, head `staging-115`, body = the rubric-shaped
   `/srv/swarm-outbox/issue-115-pr-body.md`.
2. **Issue #115 relabeled** `ready` → `in-review` (verified: `bug, p1, security,
   in-review`) — ends the per-tick re-spawn loop this file documented in §9.
3. **Evidence comment posted** on the issue, restating the live pod state and the
   operator-only remainder (§5 runbook; sync the private prelaunch twin after merge).

Re-verified first-hand at delivery, same results as §1/§9: pod `/_api/version` →
`c7270819`; `/_api/substrate` → `available_runtimes` incl. `runsc`, `runsc-hostnet`,
`container_runtime: runsc-hostnet`. Suite non-runnability re-confirmed on this account
(`ls -l /var/run/docker.sock` → `root:docker 660`, this uid not in `docker`; rootless
context at `/run/user/1018/docker.sock`; `test_daemon.py:115` hard-codes the root path;
`prelaunch` grep in the suite: 0 hits).

`ready-to-merge` deliberately NOT set on the PR: the merge-gate suite could not be executed
from this account (environmental, documented above), and the spec forbids the label on
inconclusive verification — the overseer's `test_daemon.py` run with root docker gates the
branch, as AGENTS.md assigns it.

## 11. Sixth spawn (rework lane, 2026-08-30 ~01:00 UTC): PR #134 — criterion 3 verified live

The auto-merge gate's verdict on #134 (2026-08-29T22:25Z): "evidence PASSES, but no worker
asserts issue #115's `## Acceptance` is demonstrated." This spawn is the rework lane
consuming that verdict. Everything below was run first-hand this spawn (nothing inherited).

Live pod, unchanged from §1 and re-verified now: `/_api/version` → `c7270819`;
`/_api/substrate` → `available_runtimes` `[io.containerd.runc.v2, runc, runsc, runsc-hostnet,
sysbox-runc]`, `container_runtime: runsc-hostnet` (read live from Docker `/info`,
ingress.py:531). Containment re-verified by merge-base: `51f63b91` (prelaunch wired into every
pod deploy) and `dcaad9c9` (#117 boot guard) are both ancestors of the pod's `c7270819`.

**Criterion 3 upgraded from SKIP (§4) to verified live on the pod**, no credentials needed:

- `egress-vpn` (explicit `oci_runtime: runc`, public project view) carries
  `deployed_at: 2026-08-29T19:22:17Z` — i.e. an explicit-runc image tenant was deployed
  successfully **after** the 2026-08-27 registration redeploy. A stored `deployed_at` means
  create+start succeeded: `_deploy_image` only reaches `store.save()` after
  `rtm.start_isolated()` (proxy/deploy.py) — the original failure mode (create rejected with
  `unknown or invalid runtime name`) raises before any record is kept.
- `twitter-debug` (explicit `runc`) is **serving through the public path-based ingress right
  now**: `GET /twitter-debug/` → HTTP 200, 15 068 bytes, the app's own page (title "OAuth3
debug console · Twitter"). The daemon's dead-container signature is 503
  `{"error": "image container not running"}` (no live `get_image_route`); a 200 with app
  HTML can only come from a running container.
- `browser-spi` (explicit `runc`) answers `401 {"error":"unauthorized"}` — the neko app's
  own auth response (the daemon emits `missing token` for missing auth and never the string
  `unauthorized`; proxy/ grep: 0 hits), again not the 503 dead-route signature.

Public listing (root path, `Accept: application/json`): 13 visible projects, 3 image-runtime
(the three above, all explicit `runc`), 24 hidden — no no-override image tenant exists on the
public surface to inspect for criterion 2.

Stub matrix re-run from scratch this spawn (fresh `bash:latest` container, real branch script
with only `RUNSC_SHA512` patched to the stub payload's real sha512 so the integrity gate
executes; stub `curl`/`systemctl`/`service`/`docker`; `bash -n` clean):

| scenario | `staging-115` |
|---|---|
| all three runsc runtimes registered | `done`, exit 0 |
| `runsc-hostuds`/`runsc-hostnet` missing from Docker's `Runtimes:` | `runtime 'runsc-hostuds' not registered with Docker after restart`, exit 1 |

(Staging-tip behavior — `done`, exit 0 in both — is unchanged from §7/§9's tables.)

Admin wall re-confirmed today, as documented: `POST /_api/projects` without token →
`401 {"error": "missing token"}`; SETUP-ZED.md §"No credentials" records that this machine
deliberately holds no `TEE_DAEMON_TOKEN` for the pod and no Phala/dstack CVM key, so
criterion 2's on-pod create and criterion 1b's binary-path check are structurally
operator-only (§5 runbook, one command each). Suite non-runnability unchanged
(`/var/run/docker.sock` root:docker 660, uid not in `docker`).

**Acceptance ledger after this spawn** (the ceiling of what a credentialless worker can
demonstrate): 1a ✔; 1b docker-info half ✔ live / binary-path half ✘ operator; 2 precondition
✔ live + local analog ✔ (§3c) / on-pod create ✘ operator; 3 ✔ live on the pod. Because
criterion 2's on-pod create has never been executed, a worker cannot honestly assert the
issue's acceptance is *demonstrated*, so `ready-to-merge` is not added and the `rework`
label stays — the honest-stop rule (a green label over missing evidence is the failure this
rig exists to prevent). One comment on the PR states this and names the operator's two
remaining commands.


# Issue #106 — version identity baked at build, asserted at boot — evidence (Tier 1)

All commands run on zed from the `staging-106` worktree at commit below.
Artifact NOT pushed from this box: the stored ghcr credential is read-only
("denied: permission_denied: The token provided does not match expected scopes")
and the phala API key is dead (401). Build+push+deploy are operator steps (§5).

## 1. Misbuild now fails at BUILD time (was: silent empty DAEMON_COMMIT)

`docker compose build` without GIT_COMMIT (i.e. the bare `build: .` path the issue
describes — compose args are now the only way to pass it):

```
 > [tee-daemon 7/7] RUN test -n "" || { echo >&2 "ERROR: GIT_COMMIT build arg is empty — bake the commit being built: docker build --build-arg GIT_COMMIT=<short-sha> . (or docker-compose.yaml build.args)"; exit 1; }:
0.197 ERROR: GIT_COMMIT build arg is empty — bake the commit being built: docker build --build-arg GIT_COMMIT=<short-sha> . (or docker-compose.yaml build.args)
failed to solve: process "/bin/sh -c test -n \"$GIT_COMMIT\" ..." did not complete successfully: exit code: 1
```

Plain ad-hoc `docker build` (no compose, no --build-arg — the class of script that
burned staging) fails identically, because the guard lives in the Dockerfile:

```
$ docker build -t tee-daemon:nocommit .
ERROR: failed to solve: process "/bin/sh -c test -n \"$GIT_COMMIT\" || ..." did not complete successfully: exit code: 1
```

## 2. Compose-built image serves its baked commit over HTTP

```
$ GIT_COMMIT=<commit> docker compose build
$ docker run -d --name vercheck-106 -p 18081:18081 \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e INGRESS_PORT=18081 -e DSTACK_SOCKET=/nonexistent \
    -e DAEMON_DATA_DIR=/tmp/vd -e DAEMON_AUDIT_DIR=/tmp/va -e DAEMON_TUNNEL_DIR=/tmp/vt \
    -e DAEMON_TOKEN_DIR=/tmp/vto -e DAEMON_BROKER_DIR=/tmp/vb -e DAEMON_CREDS_DIR=/tmp/vc \
    -e PROXY_SOCKET_DIR=/tmp/vp -e BROKER_SOCKET_DIR=/tmp/vbr \
    -e TEE_DAEMON_TOKEN=test-token-106 rw-106-tee-daemon:latest
$ curl -s http://localhost:18081/_api/version
{"version": "dev", "commit": "f3867048"}        # == the GIT_COMMIT passed to compose build
```

(transcript from the wiring check at staging HEAD f3867048; the pushed artifact
below is rebuilt from the branch commit after committing, same procedure)

## 3. Boot refusal when DAEMON_COMMIT is blanked by env (in-container)

An env override beats the baked value; an empty one now kills the daemon at boot
instead of 500ing per-request:

```
$ docker run --rm -e DAEMON_COMMIT= rw-106-tee-daemon:latest
RuntimeError: DAEMON_COMMIT is empty and no .git is present: this image was built
without the GIT_COMMIT build arg (docker-compose.yaml build.args, or docker build
--build-arg GIT_COMMIT=<short-sha>). Refusing to start rather than report an
unknown commit on /_api/version (issue #106).
```

## 4. Local-dev git path only where .git exists (test_daemon.py)

```
--- Test: version endpoint ---
  Version: dev commit: f3867048 ✓            # == git rev-parse --short HEAD of the running tree
--- Test: daemon refuses to boot without a commit identity ---
  Refused at boot: RuntimeError: DAEMON_COMMIT is empty and no .git is present: ... ✓
=== ALL TESTS PASSED ===
```

Full suite log: `~/paseo-batch/out/106/test.log` (not committed; box-local).

## 5. Operator steps (cannot run from zed)

The stored ghcr credential is read-only (push denied) and the phala API key on this
box is dead (401 on cvms ls); the staging compose file is not on this box
(box-inventory: operator-run). To finish acceptance (c):

1. from the merged branch:
   `docker build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) -t ghcr.io/amiller/tee-socket-proxy:staging-$(git rev-parse --short HEAD) . && docker push ghcr.io/amiller/tee-socket-proxy:staging-$(git rev-parse --short HEAD)`
   (or `GIT_COMMIT=... docker compose build` — same result, that is acceptance (a)),
2. point the staging compose at the new digest and REMOVE the hand-maintained
   `DAEMON_COMMIT=39c54cc8` literal from the daemon service `environment:` first —
   env beats the baked value and would mask the new commit,
3. `phala deploy --cvm-id webhost-staging ...`, then
   `curl /_api/version` → 200 with commit == the pushed source tree's commit.

Note: the misbuilt-image guard makes step 1 fail loudly if the arg is forgotten —
that is the whole point of the change.

## 6. Re-verified after rebase onto staging (rework, 2026-08-18)

Rebased onto origin/staging (5 commits: RFC 0017 export/import, dcap-qvl quote
verification, on-chain app approval, runsc Dns, evidence cleanup). Only PLAN.md
conflicted — resolved as the union of the per-issue plan sections; the code diff
vs staging is unchanged from the original PR. At rebased commit ad12059e:

- `docker build .` and `docker compose build`, both without GIT_COMMIT: fail at
  the Dockerfile guard (exit 1), same message as §1.
- `GIT_COMMIT=ad12059e docker compose build`, then real HTTP:
  `curl http://localhost:18081/_api/version` → `{"version": "dev", "commit": "ad12059e"}` (HTTP 200).
- `docker run --rm -e DAEMON_COMMIT= ...` → the §3 boot refusal, verbatim.
- Full `test_daemon.py` under flock: `=== ALL TESTS PASSED ===`, including the
  version test pinning `/_api/version`'s commit to the running tree (`ad12059e`)
  and the boot-refusal test. Log: `~/paseo-batch/out/106/test-rebased.log`
  (box-local, not committed).

§5 operator steps are unchanged.

## 7. SHA correction: verification re-anchored at the pushed head 856f3638 (rework, 2026-08-18)

§6 ran at `ad12059e`, which was never pushed: appending §6 amended the commit, so
the branch landed as `856f3638` (single commit on origin/staging-106). `ad12059e`
still exists in the local repo and `git diff ad12059e 856f3638` is exactly §6 —
no code difference. The §6 suite run therefore covered this tree minus this
evidence file. Re-anchored at the pushed head:

- `GIT_COMMIT=856f3638 docker compose build` → succeeds.
- bare `docker compose build` → exit 1 at the Dockerfile guard, §1's message verbatim.
- image run on :18081, real HTTP: `curl http://localhost:18081/_api/version` →
  `{"version": "dev", "commit": "856f3638"}` (HTTP 200).
- `docker run --rm -e DAEMON_COMMIT= ...` → the §3 boot refusal, verbatim.


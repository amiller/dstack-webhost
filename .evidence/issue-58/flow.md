# Issue #58 — scoped per-project debug sessions — Tier-1 evidence (rework, 2026-08-30)

Demonstrated end-to-end over HTTP against a daemon running this PR's exact head
(`staging-58` @ `594224d8`, resolved from git at boot, see the version pin below),
with real docker containers. Run on the swarm box: rootless docker via the
containerized runner (`tee-test-runner`, shared-`/tmp` socket topology — the same
convention as `.evidence/issue-133`), `flock /tmp/paseo-verify-135.lock` held
throughout. No CVM credentials, no host shell on any tenant, no raw dstack socket
(`DSTACK_SOCKET=/nonexistent`, broker disabled) — the debug surface is exercised
only through the daemon's authenticated `/_api`.

Full suite at the same commit: `test_daemon.py` → `=== ALL TESTS PASSED ===`
(49 tests); `pytest proxy/` → 16 passed (incl. the new `test_debug_session.py`
and the extended `test_docker_client.py`). Raw logs box-local:
`~/paseo-batch/out/58/{test_daemon.log,transcript.txt}`.

## Transcript (verbatim, script output; `dbg-alpha` = deno, isolation=container,
mode=attested; `dbg-beta` = same but mode=dev)

```text
== GET /_api/version  [identity pin]
{
 "version": "dev",
 "commit": "594224d8"
}

== POST /_api/projects/dbg-alpha/debug with NO token -> HTTP 401 {"error": "missing token"}

== POST /_api/projects (dbg-alpha, deno isolation=container mode=attested) -> HTTP 201

== POST /_api/projects (dbg-beta, deno isolation=container mode=dev) -> HTTP 201

== GET /_api/projects/dbg-alpha  [manifest BEFORE any debug activity]
{
 "name": "dbg-alpha",
 "runtime": "deno",
 "mode": "attested",
 "container_id": "",
 "commit_sha": "b7bb61bc3bce84d2a84e5ec2dfe5452003c011eb",
 "isolation": "container"
}

== GET /_api/projects/dbg-beta
{
 "name": "dbg-beta",
 "runtime": "deno",
 "mode": "dev",
 "container_id": "",
 "commit_sha": "c3dae5bd9fa1c7c121003566668d7ccc626efe6b",
 "isolation": "container"
}
== GET /dbg-alpha/ (each app writes a secret into its own dataDir) -> 'alpha up'
== GET /dbg-beta/ (each app writes a secret into its own dataDir) -> 'beta up'

== POST /_api/projects/dbg-alpha/debug {"ttl": 3600} -> HTTP 201
{"id": "d-FUIUXwk9yFRRuSmXnVRqZg", "project": "dbg-alpha", "created_at": "2026-08-30T23:04:32.556768+00:00", "expires_at": "2026-08-31T00:04:32.556768+00:00"}

== POST /_api/debug/d-FUIUXwk9yF…/exec hostname+cat -> HTTP 200
{
 "output": "d5d0d998faf1\nalpha-secret"
}

== GET /_api/debug/d-FUIUXwk9yF…/logs?tail=50 -> HTTP 200
{
 "logs": "\u0001\u0000\u0000\u0000\u0000\u0000\u0000\u001cdbg-alpha container started\n\u0002\u0000\u0000\u0000\u0000\u0000\u0000;Listening on http://0.0.0.0:3000/ (http://localhost:3000/)\n"
}

== GET /_api/debug/d-FUIUXwk9yF…/data?path=note.txt -> HTTP 200 body=b'alpha-secret'
== GET …/data?path=../../../etc/passwd (escape dataDir) -> HTTP 500 '500 Internal Server Error\n\nServer got itself in trouble'

== POST /_api/projects/dbg-beta/debug -> HTTP 201

== POST /_api/debug/d-JjZih7HQBm…/exec (beta session) -> HTTP 200
{
 "output": "32ccd657665b\nbeta-secret"
}
== scoping: alpha exec ran on 'd5d0d998faf1', beta exec on '32ccd657665b' (distinct containers, each saw only its own /data)

== DELETE /_api/debug/d-FUIUXwk9yF… -> HTTP 200 {"ok": true}
== POST …/exec after revoke -> HTTP 404 {"error": "debug session not found, expired, or revoked"}
== DELETE … again -> HTTP 404 {"error": "debug session not found"}

== POST /_api/projects/dbg-alpha/debug {"ttl": 1} -> 201 id=d-CFZL3-_r58…; sleeping 2.5s
== POST …/exec after TTL (no delete sent) -> HTTP 404 {"error": "debug session not found, expired, or revoked"}

== GET /_api/projects/dbg-alpha/audit (PUBLIC: attested project, no token) -> HTTP 200
    ('deploy', '{"name": "dbg-alpha", "mode": "attested", "source": "/tmp/tee-daemon-t', '')
    ('debug_mint', '{"session": "d-FUIUXwk9yFRRuSmXnVRqZg", "expires_at": "2026-08-31T00:0', 'd5d0d998faf1')
    ('debug_exec', '{"session": "d-FUIUXwk9yFRRuSmXnVRqZg", "cmd": ["sh", "-c", "hostname ', 'd5d0d998faf1')
    ('debug_logs', '{"session": "d-FUIUXwk9yFRRuSmXnVRqZg", "tail": 50}', 'd5d0d998faf1')
    ('debug_data', '{"session": "d-FUIUXwk9yFRRuSmXnVRqZg", "path": "note.txt"}', 'd5d0d998faf1')
    ('debug_revoke', 'd-FUIUXwk9yFRRuSmXnVRqZg', 'd5d0d998faf1')
    ('debug_mint', '{"session": "d-CFZL3-_r58RkM0Nga_Eeow", "expires_at": "2026-08-30T23:0', 'd5d0d998faf1')
    ('debug_expired', 'd-CFZL3-_r58RkM0Nga_Eeow', 'd5d0d998faf1')

== GET /_api/projects/dbg-beta/audit (authed) -> HTTP 400 (per-project audit endpoint is attested-only by design, RFC 0015)
== dbg-beta audit chain on disk (/tmp/tee-daemon-test-ynm4n_nd/audit/dbg-beta.jsonl):
    ('deploy', '{"name": "dbg-beta", "mode": "dev", "source": "/tmp/tee-daem')
    ('debug_mint', '{"session": "d-JjZih7HQBmmbvregfJGKLg", "expires_at": "2026-')
    ('debug_exec', '{"session": "d-JjZih7HQBmmbvregfJGKLg", "cmd": ["sh", "-c", ')

== GET /_api/projects/dbg-alpha  [manifest AFTER all debug activity]
{
 "name": "dbg-alpha",
 "runtime": "deno",
 "mode": "attested",
 "container_id": "",
 "commit_sha": "b7bb61bc3bce84d2a84e5ec2dfe5452003c011eb",
 "isolation": "container"
}

== GET /_api/projects/dbg-beta  [manifest AFTER]
{
 "name": "dbg-beta",
 "runtime": "deno",
 "mode": "dev",
 "container_id": "",
 "commit_sha": "c3dae5bd9fa1c7c121003566668d7ccc626efe6b",
 "isolation": "container"
}

== torn down dbg-alpha, dbg-beta

ALL ACCEPTANCE CHECKS PASSED
```

(Everything above is copied verbatim from the run's output; only the trailing
`…` in elided detail strings and session-id truncations are as printed by the
script itself.)

## Acceptance mapping (issue #58 `## Acceptance`)

| Criterion | Demonstrated by |
|---|---|
| `POST /_api/projects/<name>/debug` mints a TTL'd, audited grant giving exec, log tail and dataDir read on that one project's container, through the daemon — no host shell, no other project, no raw dstack socket | mint → 201 with id/expires_at; exec returned that container's hostname and its own `/data` content; logs tailed the container's stdout; data read returned `alpha-secret` written by the app itself. Two sessions on two projects ran on distinct containers (`d5d0d998faf1` vs `32ccd657665b`), each seeing only its own data. No token → 401. Structural (code, not runtime): the daemon path checks `tracker.is_allowed` + the `tee-daemon.project.<name>` label before any engine call (`ingress._debug_container`), the app-facing DockerProxy exec/archive deny is untouched, and `DSTACK_SOCKET=/nonexistent` in this run |
| `DELETE /_api/debug/<session>` revokes it, and a session past its TTL is refused without needing the delete | revoke → `{"ok": true}`; exec after revoke → 404; second delete → 404. Separate `ttl: 1` session: exec after expiry (no delete sent) → 404, and the refusal itself audited as `debug_expired` |
| A debug session against an `attested`-mode project is ALLOWED, and every action lands in `proxy/audit.py` under that project | `dbg-alpha` is mode=attested; its session minted, exec'd, tailed and read. Its audit chain (readable **without** a token — attested projects expose their audit per RFC 0015) contains `debug_mint, debug_exec, debug_logs, debug_data, debug_revoke, debug_mint, debug_expired`, each carrying the session id and container id. `dbg-beta`'s chain contains only its own `debug_mint/debug_exec` — no alpha events leaked in |
| Opening, using and revoking a session leaves the project's mode unchanged | manifests before/after identical: `dbg-alpha` `mode: attested`, `dbg-beta` `mode: dev`, both `isolation: container` |

## Honest notes (what this evidence does not claim)

- **Not the webhost-staging CVM.** The daemon ran locally at the PR's exact commit
  (`/_api/version` → `594224d8`). Shipping to `webhost-staging` needs a ghcr push +
  `phala deploy` (operator-gated from this box, same as issues #106/#115/#133).
- `GET …/data?path=../../../etc/passwd` fails **closed** (ValueError from the path
  guard propagates → HTTP 500, nothing read). It is not mapped to a 400 like other
  bad inputs — cosmetic inconsistency, no content leak.
- `/logs` returns docker's raw multiplexed frame bytes interleaved with the text
  (visible as `\u0001\u0000…` above) — the exec endpoint demuxes, the logs endpoint
  does not. Readable but noisy; a demux would be a small follow-up.
- Per-project audit over HTTP is attested-only by design; the dev-mode project's
  chain was read from the daemon's audit dir on disk (labeled as such above).

## Reproduction

```sh
git clone -b staging-58 <repo> /tmp/rw-135-run            # HEAD = 594224d8
flock /tmp/paseo-verify-135.lock bash -c '
  docker run --rm -v /tmp:/tmp \
    -v /run/user/1018/docker.sock:/var/run/docker.sock \
    -v /usr/bin/docker:/usr/bin/docker:ro \
    -w /tmp/rw-135-run tee-test-runner:latest \
    python3 /tmp/rw-135-tier1.py'        # script archived box-local; steps below
```

The script (archived at `~/paseo-batch/out/58/` on the swarm box) deploys
`dbg-alpha` (attested) and `dbg-beta` (dev) as isolation=container deno apps whose
handler writes `note.txt` into its own dataDir, then performs every request in the
transcript and asserts each response shown. Suite:
`… tee-test-runner:latest python3 test_daemon.py`.

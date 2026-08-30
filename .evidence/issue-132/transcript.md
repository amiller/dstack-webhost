# Issue #132 — refuse a deploy whose env carries `<redacted>` — evidence (Tier 1)

All commands run on zed against a daemon started from the `staging-132` worktree
(`python -m proxy.main`, fresh data dir, rootless docker), pinned below. The
webhost-staging **image** deploy (`ship-fix.sh staging` → ghcr push → `phala deploy`)
is operator-gated from this box: no write-scope ghcr credential and a dead phala API
key — same blocker as `.evidence/issue-106/transcript.md` §5. Local daemon pinned to
this PR's commit is the verifiable subset; the full `test_daemon.py` suite is green
on the same tree (`=== ALL TESTS PASSED ===`, 49 tests, incl. the new
`test_env_redaction_roundtrip_rejected`).

## 1. Deploy with a REAL env is unaffected (201, response redacted)

```
$ curl -s -X POST :18081/_api/projects -H "$TOK" -H 'Content-Type: application/json' -d '{
    "name": "redact132", "source": "/tmp/ev132/repo-redact132", "runtime": "deno", "entry": "server.ts",
    "env": {"GITHUB_CLIENT_SECRET": "staging-real-secret-xyz", "POLL_INTERVAL_MIN": "5"}}'
{"name": "redact132", "runtime": "deno", "entry": "server.ts", "port": 3000, "mode": "dev", "public": false, "env": {"GITHUB_CLIENT_SECRET": "<redacted>", "POLL_INTERVAL_MIN": "<redacted>"}, ... "commit_sha": "76f0fa9a0ea28a94aa577a252da61755c892f830", "tree_hash": "71d68cb53dbaddfead495c544297ba99f28c5c60", ...}
```

The deployed app echoes its env, proving the real values reached the container:

```
$ curl -s :18081/redact132/
{"secret":"staging-real-secret-xyz","poll":"5"}
```

## 2. The round-trip that destroyed oauth3's secrets on 2026-08-24

GET the project (this is what `deploy-prod-core.sh` serialized), then POST that
exact body back:

```
$ curl -s :18081/_api/projects/redact132 -H "$TOK" > fetched.json
$ cat fetched.json
{"name": "redact132", ..., "env": {"GITHUB_CLIENT_SECRET": "<redacted>", "POLL_INTERVAL_MIN": "<redacted>"}, ...}

$ curl -s -w '\nHTTP %{http_code}\n' -X POST :18081/_api/projects -H "$TOK" \
    -H 'Content-Type: application/json' -d @fetched.json
{"error": "env keys GITHUB_CLIENT_SECRET, POLL_INTERVAL_MIN carry the redaction sentinel '<redacted>' — refusing to overwrite stored secrets with it"}
HTTP 400
```

400 (not 201), and the message names both offending keys.

## 3. The stored secrets are untouched and still in effect

```
$ curl -s :18081/redact132/
{"secret":"staging-real-secret-xyz","poll":"5"}

$ python -c "import json; print(json.load(open('/tmp/ev132/data/redact132/project.json'))['env'])"
{'GITHUB_CLIENT_SECRET': 'staging-real-secret-xyz', 'POLL_INTERVAL_MIN': '5'}
```

The regression test additionally asserts the on-disk `project.json` is byte-identical
after the rejected deploy.

## 4. Version pin

```
$ curl -s :18081/_api/version
{"version": "dev", "commit": "d153c274"}
```

`d153c274` is this PR's head (`git rev-parse --short HEAD` in the worktree the daemon
ran from; the suite's `test_version` pins the full sha to the running tree).

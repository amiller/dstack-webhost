# Issue #132 — refuse a deploy whose env carries `<redacted>` — evidence (Tier 1, rework 2026-08-31)

Driver: `~/paseo-batch/out/142/drive.sh` on the box — every line below is a real HTTP call or
a real file read, and the driver hard-asserts each one (it exits non-zero on any mismatch).
One deno project whose `server.ts` echoes its env, so "the real secret reached the container"
is observed, not assumed. BEFORE = PR base `origin/staging` 3c4f1bb7 — the bug; AFTER = PR
head 8680e520 — the fix. Both runs pin daemon identity via `GET /_api/version` (identity is
read from git at boot, issue #106; `DAEMON_COMMIT` was not set for either run). The stored
manifest is hashed off disk (sha256), not taken from the API.

## BEFORE — origin/staging 3c4f1bb7 (the 2026-08-24 oauth3 incident, reproduced)

```
GET /_api/version -> 200 {"version": "dev", "commit": "3c4f1bb7"}
POST /_api/projects (real env) -> 201
  {"name": "redact142", "runtime": "deno", "entry": "server.ts", "port": 3000, "mode": "dev",
   "public": false, "env": {"GITHUB_CLIENT_SECRET": "<redacted>", "POLL_INTERVAL_MIN": "<redacted>"}, ...}
GET /redact142/ (app echoes its env) -> 200 {"secret":"staging-real-secret-xyz","poll":"5"}
stored manifest sha256[:12]: 79e09c89d5fa          (contains the real secret on disk)
GET /_api/projects/redact142 -> env in body: {'GITHUB_CLIENT_SECRET': '<redacted>', 'POLL_INTERVAL_MIN': '<redacted>'}
POST /_api/projects (the fetched body back, verbatim) -> 201
  {"name": "redact142", ..., "env": {"GITHUB_CLIENT_SECRET": "<redacted>", "POLL_INTERVAL_MIN": "<redacted>"}, ...}
stored manifest sha256[:12] now: e23b3a920a39 (was 79e09c89d5fa)   <-- the secrets were overwritten
GET /redact142/ now -> {"secret":"<redacted>","poll":"<redacted>"} <-- the oauth3-2026-08-24 incident, reproduced
POST /_api/projects (second project, no sentinel) -> 201
DELETE /_api/projects/redact142, redact142b -> 200 {"ok": true}
```

## AFTER — PR head 8680e520

```
GET /_api/version -> 200 {"version": "dev", "commit": "8680e520"}
POST /_api/projects (real env) -> 201
  {"name": "redact142", "runtime": "deno", "entry": "server.ts", "port": 3000, "mode": "dev",
   "public": false, "env": {"GITHUB_CLIENT_SECRET": "<redacted>", "POLL_INTERVAL_MIN": "<redacted>"}, ...}
GET /redact142/ (app echoes its env) -> 200 {"secret":"staging-real-secret-xyz","poll":"5"}
stored manifest sha256[:12]: 31aa95d8f825          (contains the real secret on disk)
GET /_api/projects/redact142 -> env in body: {'GITHUB_CLIENT_SECRET': '<redacted>', 'POLL_INTERVAL_MIN': '<redacted>'}
POST /_api/projects (the fetched body back, verbatim) -> 400
  {"error": "env keys GITHUB_CLIENT_SECRET, POLL_INTERVAL_MIN carry the redaction sentinel '<redacted>' — refusing to overwrite stored secrets with it"}
stored manifest sha256[:12]: 31aa95d8f825 == 31aa95d8f825 (byte-identical)
GET /redact142/ still -> 200 {"secret":"staging-real-secret-xyz","poll":"5"}
POST /_api/projects (second project, no sentinel) -> 201
DELETE /_api/projects/redact142, redact142b -> 200 {"ok": true}
```

Every `## Acceptance` line: the sentinel deploy returns **400 naming both keys**; the stored
manifest is **byte-identical** and the pre-existing env **still in effect** (the app keeps
serving the real secret); a sentinel-free deploy is **unaffected** (201).

## Full e2e suite at 8680e520

`=== ALL TESTS PASSED ===` — 49 `--- Test:` blocks, exit 0, including the new
`test_env_redaction_roundtrip_rejected` (400 + named keys + byte-identical stored
`project.json`) and the version test pinning the running tree to 8680e520. No existing test
was edited. Full log: `~/paseo-batch/out/142/test-daemon-full.log` on the box.

## Provenance / what this does not cover

Both daemons ran from the checkout inside the `tee-test-runner` container on this box's
rootless docker (the worker uid has no access to the system docker socket, and rootless
container IPs are not host-routable, so the daemon runs as a sibling of the app containers;
checkout and state dirs are bind-mounted at identical paths inside and outside). The
webhost-staging **image** ship (`ship-fix.sh staging` → ghcr push → `phala deploy`, then
`/_api/version` pinned on webhost-staging) stays operator-gated from this box — no
write-scope ghcr credential and a dead phala API key — same disclosure as PRs #140/#141;
the overseer's suite run on the system docker socket remains the authoritative gate.

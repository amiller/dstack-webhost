# Tier 1 — atomic deploy + promote with tree pin (issue #105, PR #138)

Daemon run from this branch's checkout at commit `7d561390` (rebased onto `origin/staging`
`683417a1` on 2026-09-13), `DSTACK_SOCKET=/nonexistent` (no dstack quote available locally —
same conditions as the test suite; promotion policy for a missing quote is unchanged by this
PR and was already exercised by the suite). Docker-backed: the daemon talks to a real Docker
Engine over `/run/user/1018/docker.sock` (rootless daemon; the rootful `/var/run/docker.sock`
is group-gated on this box).

`GET /_api/version` pins every response below to `7d561390`. The token in the
transcript is a throwaway local dev token (`tier1-local-105`), not a credential.

## How this was produced

```sh
cd <checkout at 7d561390>
T=$(mktemp -d)
INGRESS_PORT=18105 DAEMON_DATA_DIR=$T/projects DAEMON_AUDIT_DIR=$T/audit \
DAEMON_TUNNEL_DIR=$T/tunnels DAEMON_TOKEN_DIR=$T/tokens PROXY_SOCKET_DIR=$T/proxy \
BROKER_SOCKET_DIR=$T/broker DOCKER_SOCKET=/run/user/1018/docker.sock \
DSTACK_SOCKET=/nonexistent TEE_DAEMON_TOKEN=tier1-local-105 \
  .venv/bin/python -m proxy.main
# then the requests below, verbatim
```

(BROKER_SOCKET_DIR is required since RFC 0018; a writable dir keeps the broker off /var/run.)

Source repo for the deploys: a local bare git repo with one commit adding
`index.html` (`git init --bare` + clone + commit + push — same shape as
`test_daemon.create_test_repo`).

## Acceptance, demonstrated

1. `POST /_api/projects` with `expect_tree_hash` + `promote: true` deploys, verifies
   the hash, and promotes in the same call → `201`, `"mode": "attested"` — see
   **Matching case**.
2. A mismatch fails closed → `400` naming both hashes, project not promoted (stays
   `dev`) — see **Mismatch case**.
3. Without `promote: true`, behaviour is unchanged: plain deploy/redeploy returns
   `201` with `"mode": "dev"` — see **Baseline** and **bullet 3**.
4. The audit log distinguishes the two shapes: the atomic pair carries
   `operation: "deploy_and_promote"` on both its deploy and promote entries; the
   manual redeploy-then-promote pair carries `operation: ""` — see both **Audit**
   steps.


### Version pin (before)

```console
$ curl -s -X GET http://localhost:18105/_api/version
{"version": "dev", "commit": "7d561390"}
```

### Baseline: plain deploy serves in dev (pre-existing behaviour)

```console
$ curl -s -X POST http://localhost:18105/_api/projects -H 'Authorization: Bearer tier1-local-105' -H 'Content-Type: application/json' -d '{"name": "atomic-demo", "source": "/tmp/tee-105.uutuaiga/repos/atomic-demo.git", "runtime": "static"}'
{"name": "atomic-demo", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-13T15:09:22.272522+00:00", "image_digest": "", "source": "/tmp/tee-105.uutuaiga/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "842dc7d1169622841dbfa07abce5662302ecbdc2", "tree_hash": "c735422e0af895748c2a8a6cf3eb04ee31ff86c0", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": null, "binding": {}}
[HTTP 201]
```

### Status after plain deploy: mode=dev

```console
$ curl -s -X GET http://localhost:18105/_api/projects/atomic-demo -H 'Authorization: Bearer tier1-local-105'
{"name": "atomic-demo", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-13T15:09:22.272522+00:00", "image_digest": "", "source": "/tmp/tee-105.uutuaiga/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "842dc7d1169622841dbfa07abce5662302ecbdc2", "tree_hash": "c735422e0af895748c2a8a6cf3eb04ee31ff86c0", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": null, "binding": {}}
[HTTP 200]
```

### Matching case: deploy with expect_tree_hash=<audited hash> + promote:true (201, attested)

```console
$ curl -s -X POST http://localhost:18105/_api/projects -H 'Authorization: Bearer tier1-local-105' -H 'Content-Type: application/json' -d '{"name": "atomic-demo", "source": "/tmp/tee-105.uutuaiga/repos/atomic-demo.git", "runtime": "static", "expect_tree_hash": "c735422e0af895748c2a8a6cf3eb04ee31ff86c0", "promote": true}'
{"name": "atomic-demo", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "attested", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-13T15:09:22.325481+00:00", "image_digest": "", "source": "/tmp/tee-105.uutuaiga/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "842dc7d1169622841dbfa07abce5662302ecbdc2", "tree_hash": "c735422e0af895748c2a8a6cf3eb04ee31ff86c0", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": null, "binding": {}}
[HTTP 201]
```

### Audit: the deploy and promote entries both carry operation=deploy_and_promote

```console
$ curl -s -X GET http://localhost:18105/_api/projects/atomic-demo/audit -H 'Authorization: Bearer tier1-local-105'
[{"timestamp": 1789312162.2728598, "action": "deploy", "container_id": "", "image": "static", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"mode\": \"dev\", \"source\": \"/tmp/tee-105.uutuaiga/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"842dc7d1169622841dbfa07abce5662302ecbdc2\", \"tree_hash\": \"c735422e0af895748c2a8a6cf3eb04ee31ff86c0\", \"operation\": \"\", \"cap_add\": [], \"devices\": [], \"operator_debug\": false}", "prev_hash": "", "entry_hash": "f4ef866a62652f5d7e420e290495ff1d04516098e57f4b0c0b04aea343d550ae"}, {"timestamp": 1789312162.3259943, "action": "deploy", "container_id": "", "image": "static", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"mode\": \"dev\", \"source\": \"/tmp/tee-105.uutuaiga/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"842dc7d1169622841dbfa07abce5662302ecbdc2\", \"tree_hash\": \"c735422e0af895748c2a8a6cf3eb04ee31ff86c0\", \"operation\": \"deploy_and_promote\", \"cap_add\": [], \"devices\": [], \"operator_debug\": false}", "prev_hash": "f4ef866a62652f5d7e420e290495ff1d04516098e57f4b0c0b04aea343d550ae", "entry_hash": "7655c5796cd4f0fe9b0397a108e0e4a0f608e42f7eb8b774d63b20c93aa303ab"}, {"timestamp": 1789312162.3264482, "action": "promote", "container_id": "", "image": "", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"from_mode\": \"dev\", \"to_mode\": \"attested\", \"source\": \"/tmp/tee-105.uutuaiga/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"842dc7d1169622841dbfa07abce5662302ecbdc2\", \"tree_hash\": \"c735422e0af895748c2a8a6cf3eb04ee31ff86c0\", \"attestation_kind\": \"\", \"operation\": \"deploy_and_promote\"}", "prev_hash": "7655c5796cd4f0fe9b0397a108e0e4a0f608e42f7eb8b774d63b20c93aa303ab", "entry_hash": "6e4d7ba5c6f8cf6a072a663cc0887f2a1526d3f6d87528b9493fa6ef3419d951"}]
[HTTP 200]
```

### Acceptance bullet 3: plain redeploy still resets mode to dev (unchanged behaviour)

```console
$ curl -s -X POST http://localhost:18105/_api/projects -H 'Authorization: Bearer tier1-local-105' -H 'Content-Type: application/json' -d '{"name": "atomic-demo", "source": "/tmp/tee-105.uutuaiga/repos/atomic-demo.git", "runtime": "static"}'
{"name": "atomic-demo", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-13T15:09:22.381209+00:00", "image_digest": "", "source": "/tmp/tee-105.uutuaiga/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "842dc7d1169622841dbfa07abce5662302ecbdc2", "tree_hash": "c735422e0af895748c2a8a6cf3eb04ee31ff86c0", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": null, "binding": {}}
[HTTP 201]
```

### Acceptance bullet 4: manual promote afterwards (entries carry NO operation marker)

```console
$ curl -s -X POST http://localhost:18105/_api/projects/atomic-demo/promote -H 'Authorization: Bearer tier1-local-105'
{"name": "atomic-demo", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "attested", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-13T15:09:22.381209+00:00", "image_digest": "", "source": "/tmp/tee-105.uutuaiga/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "842dc7d1169622841dbfa07abce5662302ecbdc2", "tree_hash": "c735422e0af895748c2a8a6cf3eb04ee31ff86c0", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": null, "binding": {}}
[HTTP 200]
```

### Audit tail: manual redeploy+promote distinguishable from the atomic pair

```console
$ curl -s -X GET http://localhost:18105/_api/projects/atomic-demo/audit -H 'Authorization: Bearer tier1-local-105'
[{"timestamp": 1789312162.2728598, "action": "deploy", "container_id": "", "image": "static", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"mode\": \"dev\", \"source\": \"/tmp/tee-105.uutuaiga/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"842dc7d1169622841dbfa07abce5662302ecbdc2\", \"tree_hash\": \"c735422e0af895748c2a8a6cf3eb04ee31ff86c0\", \"operation\": \"\", \"cap_add\": [], \"devices\": [], \"operator_debug\": false}", "prev_hash": "", "entry_hash": "f4ef866a62652f5d7e420e290495ff1d04516098e57f4b0c0b04aea343d550ae"}, {"timestamp": 1789312162.3259943, "action": "deploy", "container_id": "", "image": "static", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"mode\": \"dev\", \"source\": \"/tmp/tee-105.uutuaiga/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"842dc7d1169622841dbfa07abce5662302ecbdc2\", \"tree_hash\": \"c735422e0af895748c2a8a6cf3eb04ee31ff86c0\", \"operation\": \"deploy_and_promote\", \"cap_add\": [], \"devices\": [], \"operator_debug\": false}", "prev_hash": "f4ef866a62652f5d7e420e290495ff1d04516098e57f4b0c0b04aea343d550ae", "entry_hash": "7655c5796cd4f0fe9b0397a108e0e4a0f608e42f7eb8b774d63b20c93aa303ab"}, {"timestamp": 1789312162.3264482, "action": "promote", "container_id": "", "image": "", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"from_mode\": \"dev\", \"to_mode\": \"attested\", \"source\": \"/tmp/tee-105.uutuaiga/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"842dc7d1169622841dbfa07abce5662302ecbdc2\", \"tree_hash\": \"c735422e0af895748c2a8a6cf3eb04ee31ff86c0\", \"attestation_kind\": \"\", \"operation\": \"deploy_and_promote\"}", "prev_hash": "7655c5796cd4f0fe9b0397a108e0e4a0f608e42f7eb8b774d63b20c93aa303ab", "entry_hash": "6e4d7ba5c6f8cf6a072a663cc0887f2a1526d3f6d87528b9493fa6ef3419d951"}, {"timestamp": 1789312162.3816133, "action": "deploy", "container_id": "", "image": "static", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"mode\": \"dev\", \"source\": \"/tmp/tee-105.uutuaiga/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"842dc7d1169622841dbfa07abce5662302ecbdc2\", \"tree_hash\": \"c735422e0af895748c2a8a6cf3eb04ee31ff86c0\", \"operation\": \"\", \"cap_add\": [], \"devices\": [], \"operator_debug\": false}", "prev_hash": "6e4d7ba5c6f8cf6a072a663cc0887f2a1526d3f6d87528b9493fa6ef3419d951", "entry_hash": "6fa2125a534331f410399f3994a96a83d071c51b73fb3c0810e5ea4e8df9cc8b"}, {"timestamp": 1789312162.3835638, "action": "promote", "container_id": "", "image": "", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"from_mode\": \"dev\", \"to_mode\": \"attested\", \"source\": \"/tmp/tee-105.uutuaiga/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"842dc7d1169622841dbfa07abce5662302ecbdc2\", \"tree_hash\": \"c735422e0af895748c2a8a6cf3eb04ee31ff86c0\", \"attestation_kind\": \"\", \"operation\": \"\"}", "prev_hash": "6fa2125a534331f410399f3994a96a83d071c51b73fb3c0810e5ea4e8df9cc8b", "entry_hash": "6ecd93243ce279c620f11fc65bbe292d003301555e73dfefc8aecfd023438a63"}]
[HTTP 200]
```

### Mismatch case: wrong expect_tree_hash -> 400 naming expected and actual, not promoted

```console
$ curl -s -X POST http://localhost:18105/_api/projects -H 'Authorization: Bearer tier1-local-105' -H 'Content-Type: application/json' -d '{"name": "atomic-mismatch", "source": "/tmp/tee-105.uutuaiga/repos/atomic-demo.git", "runtime": "static", "expect_tree_hash": "0000000000000000000000000000000000000000", "promote": true}'
{"error": "tree_hash mismatch: expected 0000000000000000000000000000000000000000, actual c735422e0af895748c2a8a6cf3eb04ee31ff86c0"}
[HTTP 400]
```

### Mismatch leaves the project served in dev (operator must correct/remove)

```console
$ curl -s -X GET http://localhost:18105/_api/projects/atomic-mismatch -H 'Authorization: Bearer tier1-local-105'
{"name": "atomic-mismatch", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-13T15:09:22.434653+00:00", "image_digest": "", "source": "/tmp/tee-105.uutuaiga/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "842dc7d1169622841dbfa07abce5662302ecbdc2", "tree_hash": "c735422e0af895748c2a8a6cf3eb04ee31ff86c0", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": null, "binding": {}}
[HTTP 200]
```

### Version pin (after)

```console
$ curl -s -X GET http://localhost:18105/_api/version
{"version": "dev", "commit": "7d561390"}
```


## Test suite on this box (rootless Docker)

`test_daemon.py` cannot complete on this box on **any** commit: the daemon dials
container bridge IPs directly from the host process, and under the rootless daemon
(slirp4netns) host→container-IP is unreachable — the ingress tests 504 at base and at
this head alike. Everything the feature path needs runs for real:

- `feature-tests.log` in this directory: `test_version`, `test_auth`, `test_deploy_static`,
  `test_ingress_static`, `test_git_blocked`, `test_redeploy`, `test_deploy_and_promote`
  (this PR's own test, verbatim), `test_audit_log` — ending `=== FEATURE TESTS PASSED ===`,
  `/_api/version` → `commit: 7d561390`, real Docker behind every deploy.
- The rebase-adapted `_api_deploy` paths were additionally exercised at this commit:
  `test_scoped_tokens`, `test_provisioner_flow` (RFC 0034 pending/approve/promote-refused),
  and the four multipart deploy tests — all passing, not committed as logs (scratch run;
  the feature path is what this PR's evidence pins).

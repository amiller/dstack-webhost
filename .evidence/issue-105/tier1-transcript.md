# Tier 1 — atomic deploy + promote with tree pin (issue #105, PR #138)

Daemon run from this branch's checkout at commit `da82d265`, started 2026-08-31,
`DSTACK_SOCKET=/nonexistent` (no dstack quote available locally — same conditions as
the test suite; promotion policy for a missing quote is unchanged by this PR and was
already exercised by the suite). Docker-backed: the daemon talks to a real Docker
Engine over `/run/user/1018/docker.sock` (rootless daemon on zed; the rootful
`/var/run/docker.sock` is group-gated on this box).

`GET /_api/version` pins every response below to `da82d265`. The token in the
transcript is a throwaway local dev token (`tier1-local-105`), not a credential.

## How this was produced

```sh
cd <checkout at da82d265>
T=$(mktemp -d)
INGRESS_PORT=18105 DAEMON_DATA_DIR=$T/projects DAEMON_AUDIT_DIR=$T/audit \
DAEMON_TUNNEL_DIR=$T/tunnels DAEMON_TOKEN_DIR=$T/tokens PROXY_SOCKET_DIR=$T/proxy \
BROKER_SOCKET_DIR=$T/broker DOCKER_SOCKET=/run/user/1018/docker.sock \
DSTACK_SOCKET=/nonexistent TEE_DAEMON_TOKEN=tier1-local-105 \
  .venv/bin/python -m proxy.main
# then the curl requests below, verbatim
```

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
$ curl -s http://localhost:18105/_api/version
{"version": "dev", "commit": "da82d265"}
```

### Baseline: plain deploy serves in dev (pre-existing behaviour)

```console
$ curl -s -X POST http://localhost:18105/_api/projects -H 'Authorization: Bearer tier1-local-105' -H 'Content-Type: application/json' -d '{"name":"atomic-demo","source":"/tmp/tee-105.1rZMDu/repos/atomic-demo.git","runtime":"static"}'
{"name": "atomic-demo", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-08-31T10:21:10.783784+00:00", "image_digest": "", "source": "/tmp/tee-105.1rZMDu/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "441f0637fa0b8468330ec44c3d06a6d2ad6e6811", "tree_hash": "5c3fe4693017e8e2511255a5662671da70c79c1c", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "operator_debug": false, "app_id": "", "app_pubkey": "", "binding_quote": "", "report_data": "", "attestation_kind": ""}
[HTTP 201]
```

### Status after plain deploy: mode=dev

```console
$ curl -s http://localhost:18105/_api/projects/atomic-demo -H 'Authorization: Bearer tier1-local-105'
{"name": "atomic-demo", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-08-31T10:21:10.783784+00:00", "image_digest": "", "source": "/tmp/tee-105.1rZMDu/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "441f0637fa0b8468330ec44c3d06a6d2ad6e6811", "tree_hash": "5c3fe4693017e8e2511255a5662671da70c79c1c", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "operator_debug": false, "app_id": "", "app_pubkey": "", "binding_quote": "", "report_data": "", "attestation_kind": ""}
[HTTP 200]
```

### Matching case: deploy with expect_tree_hash=<audited hash> + promote:true (201, attested)

```console
$ curl -s -X POST http://localhost:18105/_api/projects -H 'Authorization: Bearer tier1-local-105' -H 'Content-Type: application/json' -d '{"name":"atomic-demo","source":"...","runtime":"static","expect_tree_hash":"5c3fe4693017e8e2511255a5662671da70c79c1c","promote":true}'
{"name": "atomic-demo", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "attested", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-08-31T10:21:10.819904+00:00", "image_digest": "", "source": "/tmp/tee-105.1rZMDu/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "441f0637fa0b8468330ec44c3d06a6d2ad6e6811", "tree_hash": "5c3fe4693017e8e2511255a5662671da70c79c1c", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "operator_debug": false, "app_id": "", "app_pubkey": "", "binding_quote": "", "report_data": "", "attestation_kind": ""}
[HTTP 201]
```

### Audit: the deploy and promote entries both carry operation=deploy_and_promote

```console
$ curl -s http://localhost:18105/_api/projects/atomic-demo/audit -H 'Authorization: Bearer tier1-local-105'
[{"timestamp": 1788171670.7841036, "action": "deploy", "container_id": "", "image": "static", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"mode\": \"dev\", \"source\": \"/tmp/tee-105.1rZMDu/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"441f0637fa0b8468330ec44c3d06a6d2ad6e6811\", \"tree_hash\": \"5c3fe4693017e8e2511255a5662671da70c79c1c\", \"operation\": \"\", \"cap_add\": [], \"devices\": [], \"operator_debug\": false}", "prev_hash": "", "entry_hash": "ad9d88acdd0e53892a639874a11c690efd0d59fe6fe6c29b8e9aad50c486f28a"}, {"timestamp": 1788171670.8202188, "action": "deploy", "container_id": "", "image": "static", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"mode\": \"dev\", \"source\": \"/tmp/tee-105.1rZMDu/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"441f0637fa0b8468330ec44c3d06a6d2ad6e6811\", \"tree_hash\": \"5c3fe4693017e8e2511255a5662671da70c79c1c\", \"operation\": \"deploy_and_promote\", \"cap_add\": [], \"devices\": [], \"operator_debug\": false}", "prev_hash": "ad9d88acdd0e53892a639874a11c690efd0d59fe6fe6c29b8e9aad50c486f28a", "entry_hash": "3ebf2437cafb69f18dbd203dbf14a31b4d35daf5c767cb310994038f3f883693"}, {"timestamp": 1788171670.8207126, "action": "promote", "container_id": "", "image": "", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"from_mode\": \"dev\", \"to_mode\": \"attested\", \"source\": \"/tmp/tee-105.1rZMDu/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"441f0637fa0b8468330ec44c3d06a6d2ad6e6811\", \"tree_hash\": \"5c3fe4693017e8e2511255a5662671da70c79c1c\", \"attestation_kind\": \"\", \"operation\": \"deploy_and_promote\"}", "prev_hash": "3ebf2437cafb69f18dbd203dbf14a31b4d35daf5c767cb310994038f3f883693", "entry_hash": "7d0d4983794f52cf6429c45467535782076c34e529024b11da925438dec6dc2e"}]
[HTTP 200]
```

### Acceptance bullet 3: plain redeploy still resets mode to dev (unchanged behaviour)

```console
$ curl -s -X POST http://localhost:18105/_api/projects -H 'Authorization: Bearer tier1-local-105' ... -d '{"name":"atomic-demo","source":"...","runtime":"static"}'
{"name": "atomic-demo", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-08-31T10:21:10.840932+00:00", "image_digest": "", "source": "/tmp/tee-105.1rZMDu/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "441f0637fa0b8468330ec44c3d06a6d2ad6e6811", "tree_hash": "5c3fe4693017e8e2511255a5662671da70c79c1c", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "operator_debug": false, "app_id": "", "app_pubkey": "", "binding_quote": "", "report_data": "", "attestation_kind": ""}
[HTTP 201]
```

### Acceptance bullet 4: manual promote afterwards (entries carry NO operation marker)

```console
$ curl -s -X POST http://localhost:18105/_api/projects/atomic-demo/promote -H 'Authorization: Bearer tier1-local-105'
{"name": "atomic-demo", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "attested", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-08-31T10:21:10.840932+00:00", "image_digest": "", "source": "/tmp/tee-105.1rZMDu/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "441f0637fa0b8468330ec44c3d06a6d2ad6e6811", "tree_hash": "5c3fe4693017e8e2511255a5662671da70c79c1c", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "operator_debug": false, "app_id": "", "app_pubkey": "", "binding_quote": "", "report_data": "", "attestation_kind": ""}
[HTTP 200]
```

### Audit tail: manual redeploy+promote distinguishable from the atomic pair

```console
$ curl -s http://localhost:18105/_api/projects/atomic-demo/audit -H 'Authorization: Bearer tier1-local-105'
[{"timestamp": 1788171670.7841036, "action": "deploy", "container_id": "", "image": "static", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"mode\": \"dev\", \"source\": \"/tmp/tee-105.1rZMDu/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"441f0637fa0b8468330ec44c3d06a6d2ad6e6811\", \"tree_hash\": \"5c3fe4693017e8e2511255a5662671da70c79c1c\", \"operation\": \"\", \"cap_add\": [], \"devices\": [], \"operator_debug\": false}", "prev_hash": "", "entry_hash": "ad9d88acdd0e53892a639874a11c690efd0d59fe6fe6c29b8e9aad50c486f28a"}, {"timestamp": 1788171670.8202188, "action": "deploy", "container_id": "", "image": "static", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"mode\": \"dev\", \"source\": \"/tmp/tee-105.1rZMDu/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"441f0637fa0b8468330ec44c3d06a6d2ad6e6811\", \"tree_hash\": \"5c3fe4693017e8e2511255a5662671da70c79c1c\", \"operation\": \"deploy_and_promote\", \"cap_add\": [], \"devices\": [], \"operator_debug\": false}", "prev_hash": "ad9d88acdd0e53892a639874a11c690efd0d59fe6fe6c29b8e9aad50c486f28a", "entry_hash": "3ebf2437cafb69f18dbd203dbf14a31b4d35daf5c767cb310994038f3f883693"}, {"timestamp": 1788171670.8207126, "action": "promote", "container_id": "", "image": "", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"from_mode\": \"dev\", \"to_mode\": \"attested\", \"source\": \"/tmp/tee-105.1rZMDu/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"441f0637fa0b8468330ec44c3d06a6d2ad6e6811\", \"tree_hash\": \"5c3fe4693017e8e2511255a5662671da70c79c1c\", \"attestation_kind\": \"\", \"operation\": \"deploy_and_promote\"}", "prev_hash": "3ebf2437cafb69f18dbd203dbf14a31b4d35daf5c767cb310994038f3f883693", "entry_hash": "7d0d4983794f52cf6429c45467535782076c34e529024b11da925438dec6dc2e"}, {"timestamp": 1788171670.8412633, "action": "deploy", "container_id": "", "image": "static", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"mode\": \"dev\", \"source\": \"/tmp/tee-105.1rZMDu/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"441f0637fa0b8468330ec44c3d06a6d2ad6e6811\", \"tree_hash\": \"5c3fe4693017e8e2511255a5662671da70c79c1c\", \"operation\": \"\", \"cap_add\": [], \"devices\": [], \"operator_debug\": false}", "prev_hash": "7d0d4983794f52cf6429c45467535782076c34e529024b11da925438dec6dc2e", "entry_hash": "1fb5c33cc863ef06ec2541f1f6e92a9f71652a2875d21135872932dff08e1dcb"}, {"timestamp": 1788171670.8480127, "action": "promote", "container_id": "", "image": "", "image_digest": "", "detail": "{\"name\": \"atomic-demo\", \"from_mode\": \"dev\", \"to_mode\": \"attested\", \"source\": \"/tmp/tee-105.1rZMDu/repos/atomic-demo.git\", \"ref\": \"\", \"commit\": \"441f0637fa0b8468330ec44c3d06a6d2ad6e6811\", \"tree_hash\": \"5c3fe4693017e8e2511255a5662671da70c79c1c\", \"attestation_kind\": \"\", \"operation\": \"\"}", "prev_hash": "1fb5c33cc863ef06ec2541f1f6e92a9f71652a2875d21135872932dff08e1dcb", "entry_hash": "d709c7d2921c581872345434a4b4d6908ee636a770f874548d16426b8db89f97"}]
[HTTP 200]
```

### Mismatch case: wrong expect_tree_hash -> 400 naming expected and actual, not promoted

```console
$ curl -s -X POST http://localhost:18105/_api/projects -H 'Authorization: Bearer tier1-local-105' ... -d '{"name":"atomic-mismatch",...,"expect_tree_hash":"<40 zero chars>","promote":true}'
{"error": "tree_hash mismatch: expected 0000000000000000000000000000000000000000, actual 5c3fe4693017e8e2511255a5662671da70c79c1c"}
[HTTP 400]
```

### Mismatch leaves the project served in dev (operator must correct/remove)

```console
$ curl -s http://localhost:18105/_api/projects/atomic-mismatch -H 'Authorization: Bearer tier1-local-105'
{"name": "atomic-mismatch", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-08-31T10:21:10.868440+00:00", "image_digest": "", "source": "/tmp/tee-105.1rZMDu/repos/atomic-demo.git", "ref": "", "description": "", "commit_sha": "441f0637fa0b8468330ec44c3d06a6d2ad6e6811", "tree_hash": "5c3fe4693017e8e2511255a5662671da70c79c1c", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "operator_debug": false, "app_id": "", "app_pubkey": "", "binding_quote": "", "report_data": "", "attestation_kind": ""}
[HTTP 200]
```

### Version pin (after)

```console
$ curl -s http://localhost:18105/_api/version
{"version": "dev", "commit": "da82d265"}
```

## Test suite on this box (zed, rootless Docker)

`test_daemon.py` cannot complete on this box on **any** commit: the daemon dials
container bridge IPs directly from the host process, and under the rootless daemon
(slirp4netns, `--disable-host-loopback`) host→container-IP is unreachable. Verified
with a bare probe (`docker run --network bridge nginx:alpine` → curl to its IP from
the host times out), and by A/B:

- full suite at `da82d265` (this PR): dies at `test_ingress_deno` — `AssertionError: Failed: 504`
- full suite at `3c4f1bb7` (base `origin/staging`): dies at the same test with the same `504`

Everything up to and including the deno *deploy* passes (static serving, auth,
tokens, .git blocking, Playwright). The feature path itself — `test_redeploy`,
`test_deploy_and_promote` (this PR's own test, verbatim), `test_audit_log`,
`test_version` — passes end-to-end with the real Docker daemon:
`feature-tests.log` in this directory, ending `=== FEATURE TESTS PASSED ===`.

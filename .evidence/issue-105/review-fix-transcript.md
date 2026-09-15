# Review fix — RFC 0034 guard on the atomic path (PR #138, issue #105)

Review reject (2026-09-15): a create-scoped token could submit `promote: true` with the
matching tree hash on `POST /_api/projects`. `_deploy_request` ran before the provisioned
project was marked pending, so its prior-approval check (`if project.approval`) could not
catch a freshly created project, and `promote()` attested it — only afterwards was the
already-attested project marked pending. Trust-boundary regression against RFC 0034
("promote is refused while pending; the attested surface stays owner-approved").

**Fix** (`effd1172`): `_check_provision` — the gate every provisioned create already
passes before any deploy side effect — refuses `promote` with `403 {"error": "pending
approval"}`, the same status and message as `POST /promote` on a pending project. The
sanctioned path is unchanged: create (pending) → owner `approve` → per-project token
promotes.

**Regression test**: `test_provisioner_atomic_promote_refused` — a create-scoped token
deploys a tarball project, reads its `tree_hash`, then attacks with the same tarball, the
matching `expect_tree_hash` and `promote: true` on a fresh name.

## Pre-fix A/B (head `2d06996b` = `effd1172^`, only `proxy/ingress.py` reverted): the attack succeeds

`test_provisioner_atomic_promote_refused` run against the un-fixed daemon:

```
--- Test: provisioner atomic promote refused ---
Traceback (most recent call last):
  File "/tmp/pr138/test_daemon.py", line 1939, in test_provisioner_atomic_promote_refused
    assert resp.status_code == 403, resp.text
           ^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: {"name": "prov-atomic-2", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "attested", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-15T11:56:19.463541+00:00", "image_digest": "", "source": "tarball://local", "ref": "", "description": "", "commit_sha": "", "tree_hash": "876472769125d61764b9d11e4acec51206c1180607d079e2f325335cdf2c119e", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": {"status": "pending", "deadline": 1789473387.4685225, "created_by": "tok-CYSQCP5lboo"}, "binding": {}, "token": "tdt_tok-7RD9TSgT9Nw_GcJYSofnoCg6NOo5H6fu71xrngkX6_tuH2nG709jwsQ"}
```

The 201 body carries `"mode": "attested"` and `"approval": {"status": "pending", …}` in
the same response — attested before owner approval, then marked pending after the fact.
Exactly the reported bypass.

## Post-fix walk (head `effd1172`)

Daemon run from this branch's checkout at commit `effd1172`, same conditions as the
suite (`DSTACK_SOCKET=/nonexistent`, real Docker Engine, `BROKER_SOCKET_DIR` on a
writable tmpdir). `GET /_api/version` pins every response below to `effd1172`; the
tokens shown are throwaway local dev tokens from the run's tmpdir token store.

```
### Version pin (before)

$ GET /version
{"version": "dev", "commit": "effd1172"}
HTTP 200

### Mint a create-scoped token (RFC 0034 provisioner)

$ POST /tokens  (body: {"scope": "create", "ttl": 600, "max_pending": 2})
{"id": "tok-0n0dgmawbRs", "scope": "create", "ttl": 600, "created_at": "2026-09-15T12:01:02.414953+00:00", "expires_at": "2026-09-15T12:11:02.414953+00:00", "revoked": false, "max_pending": 2, "token": "tdt_tok-0n0dgmawbRs_-wduI-5Kur_b7ER7sGQ5zZt9GgFoeqoeP2j7g8WKNJE"}
HTTP 201

### Provisioned create, no promote (baseline)

$ POST /projects
{"name": "prov-atomic", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-15T12:01:02.417493+00:00", "image_digest": "", "source": "tarball://local", "ref": "", "description": "", "commit_sha": "", "tree_hash": "edfa1a1b65fe2b54f38e003d8d4b6e926b785afcab033e5ff946822146123b66", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": {"status": "pending", "deadline": 1790078462.4179895, "created_by": "tok-0n0dgmawbRs"}, "binding": {}, "token": "tdt_tok-KMm9jOdmfEc_3jWtDBd_rm0DAwBYsZBPEk3D5gGlqQyLRLBsLbHxnJs"}
HTTP 201
# tree_hash of the deployed tarball: edfa1a1b65fe2b54f38e003d8d4b6e926b785afcab033e5ff946822146123b66

### ATTACK: same tarball, matching expect_tree_hash, promote: true, fresh name

$ POST /projects
{"error": "pending approval"}
HTTP 403

### The attack left nothing behind (owner view)

$ GET /projects/prov-atomic-2
{"error": "not found"}
HTTP 404

$ GET /projects/prov-atomic
{"name": "prov-atomic", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-15T12:01:02.417493+00:00", "image_digest": "", "source": "tarball://local", "ref": "", "description": "", "commit_sha": "", "tree_hash": "edfa1a1b65fe2b54f38e003d8d4b6e926b785afcab033e5ff946822146123b66", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": {"status": "pending", "deadline": 1790078462.4179895, "created_by": "tok-0n0dgmawbRs"}, "binding": {}}
HTTP 200
# name=prov-atomic mode=dev approval={'status': 'pending', 'deadline': 1790078462.4179895, 'created_by': 'tok-0n0dgmawbRs'}

### Sanctioned path intact: owner approves, per-project token promotes

$ POST /projects/prov-atomic/approve
{"name": "prov-atomic", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "dev", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-15T12:01:02.417493+00:00", "image_digest": "", "source": "tarball://local", "ref": "", "description": "", "commit_sha": "", "tree_hash": "edfa1a1b65fe2b54f38e003d8d4b6e926b785afcab033e5ff946822146123b66", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": null, "binding": {}}
HTTP 200

$ POST /projects/prov-atomic/promote
{"name": "prov-atomic", "runtime": "static", "entry": "index.html", "port": 8080, "mode": "attested", "public": false, "env": {}, "container_id": "", "deployed_at": "2026-09-15T12:01:02.417493+00:00", "image_digest": "", "source": "tarball://local", "ref": "", "description": "", "commit_sha": "", "tree_hash": "edfa1a1b65fe2b54f38e003d8d4b6e926b785afcab033e5ff946822146123b66", "listen": {"port": 8080, "protocol": "http"}, "image": "", "image_port": 0, "volumes": [], "isolation": "shared", "env_passthrough": [], "dstack_env": {}, "oci_runtime": "", "cap_add": [], "devices": [], "egress": false, "egress_provider": false, "operator_debug": false, "approval": null, "binding": {}}
HTTP 200
# name=prov-atomic mode=attested approval=None

### Cleanup

$ DELETE /projects/prov-atomic
{"ok": true}
HTTP 200

### Version pin (after)

$ GET /version
{"version": "dev", "commit": "effd1172"}
HTTP 200

=== WALK OK: attack refused with 403 ===
```

The refused request happens in `_check_provision`, before any deploy side effect: no
project is created (`404` on the owner view), the untouched baseline stays `dev` +
pending, and after `approve` the per-project token promotes to `attested` exactly as
RFC 0034 prescribes.

## Suite at this head

`feature-tests.log` (regenerated at `effd1172`, `/_api/version` → `commit: effd1172`):
`test_version`, `test_auth`, `test_deploy_static`, `test_ingress_static`,
`test_git_blocked`, `test_redeploy`, `test_deploy_and_promote`, `test_scoped_tokens`,
`test_provisioner_flow`, `test_provisioner_atomic_promote_refused`, `test_audit_log`,
`test_teardown` — `=== FEATURE TESTS PASSED ===`. The #105 acceptance flows documented
in `tier1-transcript.md` (pinned at `7d561390`, an ancestor of this head) are among
them and re-verified at this head. The deno/image ingress tests still cannot run on
this box (rootless Docker: host cannot dial container bridge IPs; fails on base too —
see `tier1-transcript.md`).

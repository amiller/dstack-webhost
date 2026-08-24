# Tier 1 transcript — durable per-app version ledger (issue #61, PR #118)

- Daemon under test: commit **0336a7b3** (PR #118 head), image
  `ghcr.io/amiller/tee-socket-proxy@sha256:ca09b04f7b87ee36023da08a85f50f28ea8299c14f9ea14ef9d2cdfa1dd21d73`
  (tag `staging-0336a7b3`), deployed to the **webhost-staging CVM** (RFC 0023 loop-owned staging).
- Staging base URL: `https://78ffc78c25e0c8a9e64bb3a969ba6f226abae62d-8080.dstack-pha-prod7.phala.network`
- Test project `ledger-demo`, static runtime deployed via multipart tarball
  (`source: tarball://local`), synthetic commit ids `deadbeef0001..0004`.
- Deployment note: staging's `DAEMON_AUDIT_DIR` was pointed at a fresh
  `/var/lib/tee-daemon/audit-v2` for this deploy. The volume's legacy
  `/var/lib/tee-daemon/audit` holds ~16,800 pre-feature entries (no `entry_hash`)
  which this PR fails closed on by design (see PR Risk section); they were left
  untouched for operator-side migration, and the daemon crash-loops if pointed
  back at them — verified by design reading of `_load_entries`/`replay_anchors`,
  not by bricking the box.

## A. Deploy → history (staging, 2026-08-20 ~19:29 UTC)

```
> GET /_api/version
< HTTP 200  {"version": "dev", "commit": "0336a7b3"}

> POST /_api/projects  (multipart: manifest + app.tar.gz, commit_sha=deadbeef0001)
< HTTP 201  {"name":"ledger-demo","mode":"dev","commit_sha":"deadbeef0001",
             "tree_hash":"3f76d0c2…","source":"tarball://local","ref":"tier1"}

> GET /_api/projects/ledger-demo/history   (authed)
< HTTP 200
{"project":"ledger-demo","tamper_evident":true,"anchor_status":"rtmr",
 "attestation_replayed":true,
 "versions":[{"sequence":0,"action":"deploy","mode":"dev","source":"tarball://local",
   "ref":"tier1","commit_sha":"deadbeef0001","tree_hash":"3f76d0c2…",
   "current":true,"entry_hash":"b3deafab…"}]}
```

`anchor_status: "rtmr"` on a successful deploy is itself evidence EmitEvent
anchoring worked: under this commit a failed EmitEvent propagates and fails the
deploy (`_extend_rtmr` raises; no swallow path remains).

## B. Redeploy → prior vs current version distinguished (staging)

```
> POST /_api/projects  (commit_sha=deadbeef0002, different content → different tree_hash)
< HTTP 201

> GET /_api/projects/ledger-demo/history   (authed)
< HTTP 200
… versions[0]: commit_sha=deadbeef0001, current=false, entry_hash=b3deafab…
  versions[1]: commit_sha=deadbeef0002, tree_hash=5bb8eea2…, current=true,
               entry_hash=5c9657ed…
```

## C. Promote / unpromote transitions + public verifier surface (staging)

```
> POST /_api/projects/ledger-demo/promote   (authed)
< HTTP 200  mode=attested, app_pubkey=0381c339…, binding_quote=04000200… (TDX quote present)

> GET /_api/projects/ledger-demo/history    (PUBLIC — no token)
< HTTP 200  full ledger incl. the promote entry, mode=attested

> POST /_api/projects/ledger-demo/unpromote (authed)
< HTTP 200  mode=dev

> GET /_api/projects/ledger-demo/history    (authed)
< HTTP 200  … sequence grows: deploy, deploy, promote, unpromote …
```

Final pre-restart ledger (9 entries), authed view:

```
seq action    mode      commit_sha      current
0   deploy    dev       deadbeef0001    false
1   deploy    dev       deadbeef0002    false
2   promote   attested  deadbeef0002    false
3   unpromote dev       deadbeef0002    false
4   deploy    dev       deadbeef0003    false
5   promote   attested  deadbeef0003    false
6   unpromote dev       deadbeef0003    false
7   deploy    dev       deadbeef0004    false
8   promote   attested  deadbeef0004    true
```

## D. Restart / recovery (staging)

1. **Unscheduled hard CVM reboot** occurred mid-transcript (between C and the
   next public read; `phala cvms list` later showed fresh uptime). Effect: the
   in-flight test project's *manifest* write was lost (authed GET → 404;
   unpromote/teardown of the missing name → 500), while the **ledger survived
   intact and valid** — after redeploying the project, history still showed all
   pre-reboot entries with an unbroken chain. Observation, not a claim: manifest
   and audit writes are not jointly crash-atomic.
2. **Controlled restart** (`phala cvms restart webhost-staging`):

```
> GET /_api/version                                   (after restart)
< HTTP 200  {"version":"dev","commit":"0336a7b3"}

> GET /_api/projects/ledger-demo/history   (authed)
< HTTP 200  {"n":9,"last":{"action":"promote","mode":"attested",
             "commit_sha":"deadbeef0004","current":true},
             "anchor_status":"rtmr","attestation_replayed":true}

> GET /_api/projects/ledger-demo/history    (PUBLIC)
< HTTP 200
```

`attestation_replayed: true` after boot = `replay_anchors()` re-extended RTMR
with the prior entries, so attestation evidence covers pre-reboot history, not
only the current boot.

## E. Anchor failure fails closed (staging probe + same-image local)

- **Staging probe**: the compose temporarily mounted a regular file over
  `/var/run/dstack.sock` (path present, not a socket). The daemon never became
  reachable; the CVM ended up `stopped` and was restored by redeploying the
  normal compose. Root cause captured by running the same image locally with the
  same dead-socket mount:

```
File "/app/proxy/main.py", line 127, in start
    await broker_store.recover()
File "/app/proxy/broker.py", line 330, in recover
    await self._ensure_seal_key()
File "/app/proxy/broker.py", line 153, in _ensure_seal_key
    raise RuntimeError("dstack not available — cannot seal grants") from e
RuntimeError: dstack not available — cannot seal grants     → container Exited (1)
```

  i.e. a dead anchor path leaves the daemon loudly down, never silently serving.
- **No-socket mode** (same image, `DSTACK_SOCKET` absent): deploys succeed and
  every history response reports `"anchor_status": "unavailable"` — untrusted
  state explicitly surfaced to verifiers.
- Not demonstrated: a per-deploy 500 from EmitEvent alone with everything else
  healthy — GetKey (broker seal) and EmitEvent share one socket, so that split
  cannot be constructed with the mounts available. The propagation path is the
  code change itself (`_extend_rtmr` raises on non-200/connect failure).

## F. Tamper detection (exact deployed image, run locally on zed docker)

Staging's ledger files are not reachable from this environment (no CVM file
access), so the tamper cases were run against the **same image digest** as
staging (`staging-0336a7b3`), which self-reports `commit: 0336a7b3`:

```
> GET /_api/version
< HTTP 200  {"version":"dev","commit":"0336a7b3"}
deploy ledger-tamper v1 (cafe0001) → 201; v2 (cafe0002) → 201
history: 2 entries, entry_hashes 4fa8c221…, 800dd00c… (chained)

--- edit one entry's commit in the .jsonl (cafe0001 → cafe0099):
> GET /_api/projects/ledger-tamper/history
< HTTP 500   daemon log: ValueError: Audit entry tampered for ledger-tamper
> POST /_api/projects (redeploy)
< HTTP 400   {"error": "Audit entry tampered for ledger-tamper"}

--- restore the edit: history → HTTP 200, chain valid again

--- delete the last .jsonl line (head file still commits the old head):
> GET /_api/projects/ledger-tamper/history
< HTTP 500   ValueError: Audit ledger truncated for ledger-tamper
> POST /_api/projects (redeploy)
< HTTP 400   {"error": "Audit ledger truncated for ledger-tamper"}
```

## Observations for the reviewer (not blockers, flagged honestly)

1. A redeploy rejected with 400 (tampered/truncated ledger) still leaves the
   project manifest advanced to the new commit_sha — verified on the image
   (`project.json` showed `cafe0005` after the 400). The ledger refuses the
   append, but the current-version marker desyncs until the ledger is repaired.
2. The unscheduled reboot above lost the manifest but not the audit ledger (D.1).
3. `unpromote`/`teardown` of a nonexistent project return 500 (unhandled
   FileNotFoundError), matching teardown's pre-existing shape.

## Environment restore

- `ledger-demo` torn down on staging (teardown 200); 51 pre-existing projects
  untouched; CVM running the pinned `0336a7b3` image with the normal compose
  (real dstack socket, `DAEMON_AUDIT_DIR=/var/lib/tee-daemon/audit-v2`).
- Local containers/worktrees removed.

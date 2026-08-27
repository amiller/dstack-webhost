# Tier 1 transcript — runsc tenants reach the broker via `runsc-hostuds` (issue #6, PR #127)

- CVM: **webhost-staging** (RFC 0023 autonomous staging), redeployed
  2026-08-27 with `--pre-launch-script examples/runsc-prelaunch/prelaunch.sh`
  **from this PR branch** — the deploy that registers `runsc`, `runsc-hostuds`
  (`--host-uds=open`) and `runsc-hostnet` in the CVM's `/etc/docker/daemon.json`.
  The CVM rebooted once under load after the demo and came back clean with the
  runtimes still registered (the pinned-hash boot fix holding).
- Daemon under test: image `ghcr.io/amiller/tee-socket-proxy@sha256:ca09b04f…`
  (the compose-pinned staging build, commit **0336a7b3** — staging WITHOUT this
  PR's 4-file diff; see "What this does NOT pin" below). The staging compose was
  also corrected to the PR #81 broker contract (`/var/run/tee-broker` host bind +
  `BROKER_HOST_PATH`) — without it attested apps get no broker mount at all.
- Staging base: `https://78ffc78c25e0c8a9e64bb3a969ba6f226abae62d-8080.dstack-pha-prod7.phala.network`
- Probe tenants: two identical Deno apps (`.evidence/issue-6/probe-server.ts`,
  deployed by multipart tarball, `mode: attested`, `isolation: container`),
  differing only in `oci_runtime`: `runsc-hostuds` vs plain `runsc`.
  A successful 201 deploy under `oci_runtime: runsc-hostuds` is itself proof the
  runtime is registered — Docker rejects creates naming an unknown runtime.

## What the transcript shows (`.evidence/issue-6/staging-transcript.txt`)

1. **The acceptance call completes.** From *inside* the `runsc-hostuds` tenant,
   `GetKey {"name":"issue6-demo"}` over `/run/broker/dstack.sock` → HTTP 200
   with a dstack-derived key and signature chain. Same app under plain `runsc`:
   `ConnectionRefused (os error 111)` on every call — the connect(2) failure
   (curl exit 7 family) the issue names. The success/failure pair proves both
   that gVisor is active and that `--host-uds=open` is the load-bearing flag.
2. **The broker stays in the path.** `DeriveKey` → HTTP 403 "Method DeriveKey
   not permitted" from *inside the tenant*; legacy `path`-mode GetKey → 400.
   The tenant's only dstack channel is the filtered per-project socket.
3. **Audit surface.** `GET /_api/projects/issue6-hostuds/audit` (public,
   attested) returns the hash-chained deploy entry; `GET /_api/attest/…`
   returns the RTMR signature chain. Note: per-`GetKey`-call usage lines do not
   exist in ANY version of the dstack proxy (denials are log.warning'd; allowed
   calls forward silently) — flagged to the operator in the PR, not papered over.

## Reproduce

```bash
# probe app + manifests: .evidence/issue-6/probe-server.ts
curl -X POST "$B/_api/projects" -H "Authorization: Bearer $TOKEN" \
  -F 'manifest={"name":"issue6-hostuds","runtime":"deno","entry":"server.ts","mode":"attested","isolation":"container","oci_runtime":"runsc-hostuds","listen":{"port":8080},"source":"tarball://issue6-probe","ref":"pr127"};type=application/json' \
  -F 'files=@app.tar.gz;type=application/gzip'
curl "$B/issue6-hostuds/"
```

## What this does NOT pin

`/_api/version` reports `0336a7b3` (the deployed staging build), **not** this
PR's `f92ac371`: pushing the new image needs a ghcr write credential that is
operator-only from this box (`docker push` denied; `gh` token lacks
`write:packages`). The PR's daemon-side delta beyond the deployed build is a
single line (`runtime.startswith("runsc")` for Docker Dns on runsc variants —
not exercised by this unix-socket flow; unit-tested in
`proxy/test_docker_client.py`). The PR's load-bearing change — the
`runsc-hostuds` registration — IS the deployed, demonstrated part.

## Operational note (watch this)

Two concurrent gVisor tenants plus the pre-existing tenant fleet (24 containers)
rebooted this tdx.small CVM (1 vCPU / 2 GB) once, ~1 minute after both probes
were up. It recovered unattended. The contrast probe (`issue6-plain`) was torn
down after capture; `issue6-hostuds` is left deployed for review.

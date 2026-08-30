# Issue #22 plan

- [x] Restructure `rfcs/0019-managed-hosting-teesql.md` into reviewable form: named
      Motivation, Design (with TeeSQL data model + AttestMesh topology subsections),
      Deployment story, and Open Questions sections, each substantive, preserving the
      existing pitch content (Context, what each side brings, options a/b, constraints).
- [x] No TODO/TBD placeholders anywhere in the RFC.
- [x] End the RFC with an MVP slice section naming the first implementable step
      (option (a) pilot: one managed base-prod dstack node, mesh unused, RFC 0017
      durability floor) and its own checkable acceptance criteria, so the next
      tracking issue can be filed against it.
- [x] Tier 0: documentation only, no behavior change — no code touched.

Operator verification remains: review the rendered RFC; the follow-up tracking issue
against the MVP slice is the operator's to file.

---

# Issue #2 — isolation:container tenants have no outbound DNS under runsc

> Verbatim plan record: "tenant" throughout this section means an app (a project's container).

PLAN — checkboxes derived from the issue's `## Acceptance`.

## Root cause recap
Docker forces its embedded resolver `127.0.0.11` on user-defined bridges. Under runc, host
iptables DNAT makes it reachable; under gVisor the Sentry's netstack never applies those rules,
so `127.0.0.11` is a dead address → every hostname `fetch()` fails → ingress 500.

## Where the fix goes (differs from the issue's *proposed* location — acceptance is what binds)
The issue proposes `docker_proxy.py::_handle_create`, but daemon-created tenant containers never
traverse that handler: `proxy/main.py` builds `DockerClient(DOCKER_SOCK)` (the REAL socket) and
`runtimes.py::start_isolated` / `start_image` create containers through it directly. The proxy
handler only serves the tenant-facing `PROXY_SOCKET_DIR/docker.sock`. The real chokepoint for
the symptom is `docker_client.py::create_container` — every daemon tenant create flows through
it (isolated, image-runtime, browser pool).

## Diff
- [x] `proxy/docker_client.py`: when `runtime == "runsc"`, set `HostConfig.Dns` to routable
      resolvers `["8.8.8.8", "1.1.1.1"]` (constant `GVISOR_DNS`). runc/shared keeps Docker's
      embedded resolver byte-identical to today (no behavior change where nothing was broken).
- [x] Update the now-stale shared-runtime comment in `proxy/runtimes.py` (it cites the DNS
      breakage as a reason shared stays runc; after the fix the co-trust rationale remains).
- [x] `proxy/test_docker_client.py`: unit test — runsc create carries `Dns`, runc/shared does
      not (gating is the safety property; runsc is not installed on this box).
- [x] `test_daemon.py::test_dns_probe`: acceptance-shaped e2e — same fetch-handler source
      deployed `isolation:container` AND `isolation:shared` both serve 200; isolated tenant
      keeps its `tee-proj-*` bridge (inspect).

## Evidence (Tier 1 — container/network behavior over HTTP, no UI)
- [x] BEFORE on real runsc: deploy `dns-probe` via the webhost-staging API (unpatched daemon,
      runsc-backed) → record the 500/dns-error transcript + staging `/_api/version` pin.
- [x] AFTER mechanism on local daemon @ this PR's commit (`/_api/version` pinned): container
      tenant fetches `https://example.com` → 200 + body; shared → 200; `docker inspect` shows
      `tee-proj-*` network intact; NAT-path check: a container on a `tee-proj-*` bridge with
      `--dns 8.8.8.8` resolves + fetches (the exact mechanism the fix relies on).
- [x] Full `test_daemon.py` green under flock; log to `~/paseo-batch/out/2/test.log`.
- [x] Transcript committed at `.evidence/issue-2/tier1-transcript.txt`; PR body embeds key lines.

## Explicitly out of scope (say so, don't silently include)
- The `oci_runtime` carry-forward footgun from the issue comments — the operator marked it
  "harmless once the Dns fix lands".
- The AFTER-on-runsc 200 requires redeploying the daemon on the CVM (ghcr creds +
  `docker-compose.webhost-staging.yaml` are not on this box — box-inventory "Not here") →
  named as the overseer step in PR + issue comment.

---

# Issue #90 plan

- [x] Encode and call the configured Base-prod `DstackApp.isAppAllowed(AppBootInfo)` RPC method.
- [x] Report numeric chain ID 8453 and the actual allowlist result as facts.
- [x] Preserve non-anchored deployments as `chain_id=0`, `approved=false` without an error.
- [x] Surface RPC failures in `OnchainFacts.error` and `Facts.errors[]`.
- [x] Include the contract, method, and arguments as an independent repro pointer.
- [x] Test approved, rejected, non-anchored, and unreachable RPC paths.

Operator verification remains: provide the base-prod RPC/contract configuration and deploy the
consumer-facing verifier path to staging for the required pinned `/_api/version` transcript.
# Issue #106 plan — version identity baked at build, asserted at boot

- [x] (a) `docker-compose.yaml` `build:` gains an `args:` block supplying `GIT_COMMIT`,
      so `docker compose build` bakes `DAEMON_COMMIT` without depending on `ship-fix.sh`.
- [x] (a+) `Dockerfile` asserts `GIT_COMMIT` is non-empty at build time — every build
      path (compose, ad-hoc `docker build`, CI) now fails loudly instead of baking `""`.
- [x] (b) `proxy/main.py` resolves the commit once at boot and **refuses to start**
      when `DAEMON_COMMIT` is empty/unset and no `.git` is present (local-dev git read
      stays, but only where `.git` exists; its absence is a hard error).
- [x] (b) `proxy/ingress.py:_api_version` drops the request-time `git rev-parse`
      fallback — identity is settled at boot or the process is already dead; a
      request-time fallback could only ever mask a broken deploy.
- [x] Tests: `test_daemon.py` pins `/​_api/version`'s commit to the running tree and
      adds a boot-refusal test (misbuilt image exits non-zero with a clear message).
- [ ] (c) Fresh staging deploy build→push→`phala deploy`→`GET /_api/version`: build
      + local HTTP serve verified; push (read-only ghcr token) and `phala deploy`
      (dead API key) are operator steps — exact commands in
      `.evidence/issue-106/transcript.md` §5.

---

# Issue #121 plan — "tenant" is used for containers; the word hides which boundary gVisor enforces

PLAN — checkboxes derived from the issue's `## Acceptance`.

- [x] isolation-probe.md: /proc finding restated as cross-**app** (not cross-tenant); a
      "which boundary" statement saying gVisor enforces app↔app + app↔host-kernel and NOT
      user↔user; full tenant→app/project word pass over the page.
- [x] No "tenant"-for-a-container left in `proxy/` comments or repo docs (root docs, rfcs/,
      examples/, AGENTS.md, docker-compose comment, prelaunch.sh). Occurrences that mean a
      *person* (browser pool, RFC 0028) become "user". Historical verbatim logs
      (`.evidence/`, SETUP-ZED.md's pasted run log) stay untouched, named in the PR.
- [x] README.md + DEVELOPER_GUIDE.md name the three layers (host owner / app / agent-user)
      once, up front.
- [x] test_daemon.py suite green (prose-only diff: comments, docstrings, print labels).
- [x] Tier 2 walk of the edited probe page copy deployed to webhost-staging (python-runtime
      tarball — the dockerfile runtime turned out to be accepted-but-unimplemented: no build/
      start path exists, so `probe-121` never left "runtime not running"; the walk serves the
      branch's probe.py + index.html verbatim behind a handle() adapter);
      ghcr probe-image rebuild for hermes-staging remains the named operator step.

---

# Issue #133 — /_api/status reports a dead container as healthy

PLAN — checkboxes derived from the issue's `## Acceptance`.

## Root cause
`RuntimeManager.get_project_liveness` was synchronous, so it could only read the
daemon's own bookkeeping: `image_routes`/`runtime_ips` (populated at deploy time
and never cleared when a container dies on its own) and `Project.container_id`
(which nothing in the tree ever writes — it is `""` for every project). RFC 0016
already required "stopping a runtime container flips that project to running:
false on the next call"; the in-memory maps cannot deliver that.

## Diff
- [x] `proxy/docker_client.py`: `container_state(name)` — one engine read by
      container *name* (never the stored id). 404 is the only "missing"; any
      other engine error raises.
- [x] `proxy/runtimes.py`: `_project_container_name` (the container that serves a
      project: `tee-image-*`, `tee-isolated-*`, `tee-runtime-*`, none for static);
      `get_project_liveness` is now async and returns
      `{running, container_id, backend, container_state, exit_code, restart_count}`.
      The dockerfile branch (running := bool(stored container_id)) is gone: nothing
      ever creates a container for a dockerfile project, so it reports `missing`.
- [x] `proxy/ingress.py`: `_api_routes` and `_api_aggregate_status` await the
      helper; status joins all six liveness fields.
- [x] `proxy/test_docker_client.py`: unit tests for the inspect → status mapping
      and for "only a 404 means missing".
- [x] `test_daemon.py::test_status_live_container_state`: running / exited
      (an app whose entry throws at module load → `exit_code: 1`) / missing
      (container removed, project still listed), plus shared-runtime and static.
- [x] Existing tests unchanged and green.

## Evidence (Tier 1 — API behavior, no UI)
- [x] `.evidence/issue-133/transcript.txt`: local daemon at this commit
      (`/_api/version` → `e1becbf6`), real docker. Two apps deployed through the
      API; `docker kill` / `docker rm -f` under the daemon; `/_api/status` shows
      `running` → `exited`+`exit_code 137` → `missing`, while
      `GET /_api/projects/<name>` still returns the healthy-looking manifest the
      issue quotes. Also shows a shared-runtime project dying with its runtime
      container.
- [x] Full `test_daemon.py` green (49 tests incl. the new one) on real docker.

## Not done here (operator-gated / out of scope)
- Deploying the built image to webhost-staging: `ship-fix.sh staging` needs a ghcr
  write credential this box does not have (`docker push` → unauthorized), so the
  `/_api/version` pin above is a local daemon, not the staging CVM. Same gate as
  PRs #127/#141/#142.
- In-container healthchecks (the `egress-vpn` case) — explicitly out of scope in
  the issue.
- `GET /_api/projects/<name>` still returns the manifest only; the issue's Fix
  section names `/_api/status` and the acceptance matches, so the per-project
  endpoint is left alone.

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

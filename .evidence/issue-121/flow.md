# Issue #121 — Tier 2 flow evidence

**Acceptance asserted:** (1) isolation-probe.md states which boundary gVisor does and does not
enforce (apps yes, users no) and no longer calls the `/proc` finding cross-tenant — now
"a demonstrable **cross-app** information leak" with a "Which boundary the runtime enforces"
section; (2) no "tenant"-for-a-container left in `proxy/` or repo docs — the only remaining
occurrences are the word discussed *as a word* (isolation-probe.md:18) and verbatim historical
records, each of which now carries a one-line gloss ("means an app"); (3) README.md and
DEVELOPER_GUIDE.md each name the three layers (host owner / app / user) once, up front.

## The walk (2026-08-25, real Brave via envoy bridge, not CDP)

Deployed the branch's probe page to webhost-staging from commit `4d83bbb3` as project
`probe-121` (python-runtime tarball; `GET /_api/projects/probe-121` pins
`commit_sha: 4d83bbb3e52d6d14c2cb154e6c174babe646b945`). The page is served verbatim from this
branch (`index.html`, `probe.py`) behind a 15-line `handle()` adapter (`walk-adapter-app.py`)
because the daemon's dockerfile runtime is accepted-but-unimplemented — `VALID_RUNTIMES` takes
it, but no build/start path exists, so a dockerfile deploy sits at "runtime not running" forever
(discovered live during this walk; reported on the issue).

1. **01-page-verdict.png** — navigated to
   `https://78ffc78c25e0c8a9e64bb3a969ba6f226abae62d-8080.dstack-pha-prod7.phala.network/probe-121/`,
   asserted `location.href` before capture. The page copy shows the new wording ("an ordinary
   **app** of dstack-webhost", "**App** evidence", "co-hosted apps" — verified in-DOM that
   "Tenant evidence" is absent). The verdict rendered live from real fetches:
   `/_api/substrate` → `effective_runtime: "runc"`, and `./api/probe` → the app's own
   `/proc/self/uid_map` `0 0 4294967295`; verdict text: *"Substrate is on default runc; the
   app shows the trivial mapping and host kernel. No app-vs-app hardening from the runtime
   layer."* — i.e. the edited verdict copy, rendering a real verdict.
2. **02-app-evidence.png** — the App-evidence section, scrolled into view: the app's live
   kernel-namespace JSON (`uid_map`, `user_ns`, `pid_ns`, `mount_ns`, `cgroup`) rendered on the
   page from this branch's `probe.py`.

## What this walk does NOT prove

- The probe page here runs in the daemon's shared python container on webhost-staging (runc
  substrate), not on hermes-staging under runsc. Rebuilding `ghcr.io/amiller/tee-isolation-probe`
  from this branch and redeploying hermes-staging's `isolation-probe` project needs ghcr push
  credentials this box does not have — **operator step**, named on the issue.
- The page is public diagnostic with no identity layer, so "signed in" is not applicable; the
  walk is of a public page's value state (the rendered verdict).
- The `.md` docs render on GitHub Pages (`amiller.github.io/dstack-webhost`), which publishes
  from `main` — the rendered-docs check lands at promotion; in-PR, the markdown renders in the
  PR's Files-changed view.

`test_daemon.py`: ALL TESTS PASSED (48 tests) on this branch — `test.log`.

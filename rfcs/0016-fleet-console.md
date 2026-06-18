# RFC 0016: Fleet Console and Status Surface

## Summary
A single agent-readable status endpoint, a minimal HTML console, and a browser-friendly per-app verifier page — so the operator (and Claude) can see at a glance what is deployed, what version it is pinned to, whether it is actually running, and so an attested app can be *shown* to a relying party via a URL.

## Problem
The pinning data already exists. `proxy/projects.py` records `commit_sha`, `tree_hash`, `image_digest`, `deployed_at`, `source`, `ref`, `runtime`, and `mode` per project in `project.json`. But the only way to read it is `GET /_api/projects/<name>`, which returns raw JSON behind the admin token, one project at a time. There is:

- **No aggregate view.** `GET /_api/projects` lists projects and `GET /_api/routes` lists routes, but neither joins manifest metadata with live container state into one picture.
- **No liveness/health signal in the manifest view.** `GET /_api/routes` already distinguishes a running backend from "runtime not running" by inspecting `rtm.image_cids` and the runtime containers — but `GET /_api/projects` does not carry that, so you can't tell from the inventory whether an app is actually up.
- **No human-facing presentation.** RFC 0015 made `/_api/attest/<name>` and `/_api/verification/<name>` public for attested projects, but they return JSON. There is no page you can hand someone that renders "this app is running commit X / tree Y, here is the dstack quote, here is the audit trail." (The old `apps/router-dashboard` was a personal app, not a platform demo, and now lives in the separate `webhost-apps` repo.)

The result: the operator cannot answer "what do I have running and at what version" without curling per-project JSON, and cannot show an attested app to anyone without walking them through API calls.

## Files to Modify
- `proxy/ingress.py` — new `GET /_api/status` aggregator; serve the console HTML; add an HTML rendering of the existing verification bundle.
- `proxy/runtimes.py` — factor the running/backend resolution used by `GET /_api/routes` into one helper so status and routes share a single source of truth for liveness.
- New static asset (`proxy/console.html` or under `assets/`) for the console page.

## Implementation
1. **One liveness helper.** Extract the logic `GET /_api/routes` uses to resolve a project to `{running, container_id, backend}` (the same check that emits "runtime not running") into a function in `runtimes.py`. Both routes and status call it — no second copy of the liveness rule.
2. **`GET /_api/status`** (authed, same gate as the `GET /_api/projects` list). For each project from `ProjectStore.list()`, return the manifest fields a human cares about — `mode`, `runtime`, `source`, `ref`, `commit_sha`, `tree_hash`, `image_digest`, `deployed_at`, `port`/`listen` — joined with the live `{running, container_id, backend}`. For attested projects, include the public verification URL.
3. **Console page** (`GET /_api/console`, authed): a single static HTML page that fetches `/_api/status` and renders a table grouped by `mode`, with a health dot (running/stopped), short commit + short digest, relative `deployed_at`, and links to the app URL, the verification page (attested only), and the audit log. No framework — plain HTML + fetch, matching the existing app style.
4. **Per-app verifier page** — the "show someone" deliverable. Add an HTML rendering of the *existing* RFC 0015 verification bundle, e.g. `GET /_api/verification/<name>?format=html` (public for attested, same rule as the JSON form). It renders manifest + quote + audit into a readable page: what commit is running, the tree hash to check against GitHub, the dstack quote, and the audit log since promotion. This adds **no new trust data** — it is a view over what 0015 already serves.

## Testing & Validation Requirements
- `GET /_api/status` lists every project with a correct `running` flag; stopping a runtime container flips that project to `running: false` on the next call.
- The `running` flag for a given project agrees with what `GET /_api/routes` reports (shared helper).
- For an attested project, the verifier page returns 200 to an anonymous caller; for a dev project it 404s (consistent with RFC 0015).
- The console renders the table and the health dots from a live `/_api/status`.

## Report Requirements
- `curl /_api/status` output for a mixed dev/attested fleet.
- The rendered console (HTML or screenshot) and a rendered verifier page for one attested app.
- Diff showing the liveness helper is defined once and used by both `/_api/routes` and `/_api/status`.

## Out of Scope
- Historical metrics, time-series, or graphs (request logging is RFC 0006).
- Any write/mutating action from the console — it is read-only; deploy/teardown stay on the existing API.
- A management-API auth UI (token is fine, per RFC 0001 non-goals).

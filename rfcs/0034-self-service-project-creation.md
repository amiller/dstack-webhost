# RFC 0034: Self-Service Project Creation (provisioner token + pending approval)

**Status**: Implemented (2026-09-09) — `proxy/pending.py`; `DAEMON_PENDING_TTL`, `DAEMON_SWEEP_INTERVAL`, `DAEMON_NOTIFY_HOOK`; test `test_provisioner_flow`

## Summary
Let a new project be created on the pod without the owner token. The owner mints one
long-lived **provisioner** token whose only power is "create a project that doesn't exist
yet". Creating with it returns a fresh per-project token, deploys the app immediately in
`dev` mode, and marks it **pending** for seven days. The owner is notified, sees the
pending list, and approves or lets it freeze. Deploy scripts end up holding a key that
opens one project; the owner token stays on no script at all.

## Problem
Start from what the daemon does today. `_check_auth` (`ingress.py:510`) accepts either the
owner token or a scoped token from `TokenStore`. A scoped token's scope is a path prefix:
`projects/feedling-web` allows `GET/DELETE /_api/projects/feedling-web` and the `redeploy`,
`promote`, `logs` routes under it. That is the whole of issue #18, and it works.

Now try to use it for a new project. Creation is `POST /_api/projects`, whose path is
`projects`, not `projects/<name>`, so `scope_allows` says no for every per-project scope.
The only scopes that pass are `projects` itself (every project, create included, promote
included) and the owner token. Either way the thing that creates a project can also
delete every other one. So in practice every project starts its life by copying the owner
token into its deploy script. On this laptop that is five scripts holding
`TEE_DAEMON_TOKEN` verbatim (`deploy-prod-feedling.sh`, `attestmesh-audit/deploy-staging.sh`,
`matrix-greeter/deploy.sh`, `teleport-arena-shop3d/game/deploy.sh`, the hermes tee-daemon
skill), and zero holding a scoped one. Nobody mints scoped tokens because by the time you
could, the owner token is already in the file.

The result is the worst of both: creating a project is high-friction (find the master
key, get the agent past the "don't copy credentials" rule) and low-security (the copy
you make is the master key). What we want is the reverse. Making a project should be one
command with no ceremony, and the key that command leaves behind should open only that
project.

## Design

### 1. A `create` scope
Add one scope, `create`, to `normalize_scope` (today that string normalizes to `projects/create`,
a project named "create", so the name joins the reserved list). A token with it can do exactly one thing:
`POST /_api/projects` for a `name` that does not exist. Any other path returns 403 as now.
The owner mints it once:

```sh
curl -X POST $CVM/_api/tokens -H "Authorization: Bearer $OWNER" \
  -d '{"scope":"create","ttl":31536000,"max_pending":5}'
```

and puts the result in one place on the laptop (`~/.config/dstack-webhost/provisioner`).
That file is what every future `deploy.sh` reads to create. Nothing else reads the owner
token.

### 2. Create returns a per-project token
When the daemon accepts a create from a `create` token, it also mints a
`projects/<name>` token (default ttl one year) and returns it in the 201 body:

```json
{"name":"hello-pending","mode":"dev","approval":{"status":"pending","deadline":"2026-09-16T…","created_by":"tok_3f…"},
 "token":"pt-…"}
```

The deploy script writes that token next to the project (`.webhost-token`, gitignored)
and every later `redeploy`, `logs`, `promote` uses it. This is attenuation: owner mints
provisioner, provisioner begets per-project. The per-project token is shown once; the
daemon stores its hash, same as today.

### 3. Pending
A project created by a `create` token carries `approval` in `project.json`:

- `status: pending`, `deadline: created + 7d`, `created_by: <token id>`.
- It deploys and serves at `/<name>/` immediately. Pending never blocks serving; the
  point is that a new app works within a minute of the idea.
- `promote` is refused while pending (403 `pending approval`). The attested surface
  stays owner-approved.
- At the deadline, an unapproved project is **frozen**: container stopped, files and
  volume kept, ingress answers 503 `pending expired`. Freeze rather than delete, so a
  week of inattention costs a restart, not the work.
- `POST /_api/projects/<name>/approve` (owner only) clears `approval` and unfreezes.
  `DELETE` works as before.

### 4. Bounded blast radius
`max_pending` (default 5) caps how many unapproved projects one `create` token can have
at once; the sixth create returns 429. So a leaked provisioner token buys an attacker five
dev-mode apps that freeze in a week, and a notification in the owner's inbox. Revoking it
is the existing `DELETE /_api/tokens/<id>`.

### 5. Notify
The daemon runs `DAEMON_NOTIFY_HOOK` (an executable, or a URL if it starts with `http`)
with one JSON envelope on stdin for `create`, `approve`, and `freeze` events. Same shape
capdel uses for `CAPDEL_ESCALATE_HOOK`: fire-and-forget, ten-second timeout, failures
audited and never blocking. `GET /_api/projects?pending=1` lists the queue for the owner;
the RFC 0033 owner login shows the same list on the viewer with an approve button.

## Walkthrough, from the owner's seat
You have an idea for an app. You write `server.ts` in a new folder and run `deploy.sh`.
The script reads the provisioner file, POSTs the tarball, and prints a URL. You open it;
it works. Your phone shows "hello-pending created from laptop, approve by Sep 16". Six
days later you either tap approve, or you don't and the app stops until you do. At no
point did anything on the laptop other than one file in `~/.config` hold a key that could
touch feedling-web. `examples/hello-pending/` is this script and handler; run it against
a daemon with this RFC implemented and the transcript above is what you see.

## What this does not do
- It does not remove the owner token from the laptop. It removes it from deploy scripts.
- It does not change the shared runtime's env handling (RFC 0018 covers secrets that
  apps *use*; this RFC covers the key that *deploys* them).
- Pending is per-pod. Cross-pod approval belongs with RFC 0024.

## Relation
- Issue #18 shipped `TokenStore`; this RFC adds one scope and one field to it.
- RFC 0018 grants are the same mint/attenuate/revoke shape one layer down.
- RFC 0033 is where the approve button lives.
- capdel's mint/attenuate/escalate/approve is the same protocol; the pod is a broker
  whose only capability type is "project".

## Acceptance
1. A `create` token cannot read, redeploy, promote, or delete any existing project.
2. Create with it returns a `projects/<name>` token that can.
3. `promote` on a pending project is refused.
4. An unapproved project is frozen at the deadline and unfrozen by `approve`.
5. The sixth simultaneous pending create from one token is refused.
6. `examples/hello-pending/deploy.sh` runs end to end with no owner token in its env.

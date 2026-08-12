# AGENTS.md — how to work in this repo

Read this before touching code. It is the operating posture for any coding agent
(local or remote). Env mechanics live in **SETUP-ZED.md**; this file is about *how to write
the change*.

## What this repo is

`dstack-webhost` (a.k.a. tee-daemon): a multi-tenant host that runs apps in a TEE and lets
their attestation be **verified**. It is a **public** repo. The design queue is `rfcs/`; the
task list extracted from those RFCs is `kanban.json`. A task usually names an RFC — **read
that RFC first**, it is the spec.

## Coding posture (this is the part that matters)

- **No fallbacks. Ever.** Propagate errors as they are; never mask, swallow, or "default
  around" a failure. This is doubly load-bearing here: a swallowed error in attestation/verify
  code becomes a silent *false* "verified" — a trust hole, not a bug. If something can't be
  done, error out loudly.
- **Root cause, not stopgap.** Don't bump a constant and leave a TODO. (This repo's own history
  has the lesson: the gVisor-wedge stopgap that just re-pinned a hash vs. the real fix that
  pinned a dated, immutable release — `git log` for `runsc-prelaunch`. Do the real one.)
- **Minimal lines.** Aggressively simplify. The smallest diff that correctly solves it wins.
  Match the surrounding code's style, naming, and idiom.
- **Comments only when the code isn't self-evident.** Don't narrate obvious code.
- **Investigate before you assert.** Read the actual module before claiming how it behaves;
  never guess at behavior or invent an API. If you're changing `ingress.py`, read it first.
- **Don't skip testing to make progress.** A change isn't done because it compiles.

## The verification split — you edit, the overseer verifies

The remote worker sandbox **blocks docker and `.git`**: you can *edit* but you cannot run the
end-to-end suite or commit. That's intentional — it forces execution-grounded review.

- Leave the working tree clean and the change self-contained. **Do not claim tests pass** —
  you can't run them. State what you changed and what *should* be verified.
- The overseer (an unsandboxed session) runs `test_daemon.py` with real docker and only commits
  what passes. Write code that makes that gate easy: clear behavior, no hidden state.
- The suite command, the Python-3.11 venv, and the `BROKER_SOCKET_DIR` gotcha are all in
  **SETUP-ZED.md** — read it once.

## Repo map (read the module before you change it)

The daemon is `proxy/`. Entry point `proxy/main.py`. Load-bearing modules: `ingress.py`
(aiohttp ingress + routing + the `/_api` dispatch + verification endpoints), `deploy.py`
(deploy/promote, records the source pins), `runtimes.py` (per-project containers, volumes,
env, `recover_all`), `projects.py` (manifest store), `audit.py` (per-project audit log),
`tunnel.py` (TTL/revocable tokens — the structural model an RFC-0018 broker would follow).
End-to-end tests: `test_daemon.py`.

## Secrets & branches

- **Never commit credentials.** No `deploy/` dir, no tokens, no `.env*`. This repo is public.
- One branch per machine; never force-push or touch another machine's branch. Push only when
  explicitly asked. (See SETUP-ZED.md for the convention.)
)

## Definition of Done for delegated work (review = read a report, not a diff)

A task is NOT reviewable until all of these exist. Do not ask a human to review without them:

1. **Deployed to staging** (webhost-staging daemon, `POST /_api/projects`). If you cannot
   deploy (no token — deploy tokens live only on the operator's laptop), say so explicitly
   and hand off to the orchestrator for the deploy+report step; do not skip to "please review".
2. **Acceptance suite green against the staging URL** — the RFC's numbered criteria run
   remotely, with explicit SKIP-notices for anything unreachable over HTTP. Exit-code semantics.
3. **A published report page** on staging (static app, single self-contained HTML) stating:
   why merge (pass/fail table), what you get (one paragraph), live endpoint samples,
   reviewer checklist + known deferred items. Example: `caps-accept-report` (RFC 0032).

The human reviews the report and the merge into main/prod — never raw work-in-progress.
Prod deploys always remain human-directed.

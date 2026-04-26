---
layout: default
title: Auditing a project
---

# Auditing a project: an agent's runbook

You arrived here from an [evidence bundle](verify.md). The bundle gave you provenance — the source running on a remote CVM matches a specific GitHub commit. Your job now is to evaluate whether that source actually does what the project claims, against an adversary that can control inputs and operate the host.

This page is structured to be runnable by an agent. Each section is a concrete step. The intent is that a competent coding agent with shell access can complete a useful first-pass audit by following it.

## What you are and aren't auditing

A project on a tee-daemon instance has two layers of code:

1. **The trusted base.** The dstack-webhost daemon, the shared language runtime (e.g. Deno) that loads the project, and the dstack platform underneath. This is the *substrate* — assume it does what its repo and platform docs claim, or audit it separately. It's the same base for every project. You don't redo this audit per project.
2. **The project under review.** The handler the project author wrote, plus whatever environment knobs they declared in their manifest. *This* is what you audit.

The point of the split: the project author can write small, custom code without forcing you to re-audit web servers, container runtimes, or the TEE platform. The base does the heavy lifting; the project is small enough to read.

For the substrate's contract, see this repo: [github.com/amiller/dstack-webhost](https://github.com/amiller/dstack-webhost). In particular `proxy/runtimes.py` defines the deno router that loads project handlers, and `proxy/templates/` has the env vars handlers receive. These are the inputs your project's handler can rely on; everything outside them is project-specific code you must read.

## Setting up a local replica

The same daemon image that runs the production CVM runs locally. A local replica gives you what production deliberately doesn't: filesystem access to the runtime container, control over time sources and storage, and live logs.

```bash
# Clone the substrate
git clone https://github.com/amiller/dstack-webhost
cd dstack-webhost

# Start the daemon. This pulls the deno runtime image on first run.
docker compose up -d

# Wait until the daemon answers
until curl -sf http://localhost:8080/_api/projects -H "Authorization: Bearer $TOKEN" >/dev/null; do sleep 1; done

# Deploy the project under review from its public source.
# Use the same commit_sha and ref recorded in the evidence bundle.
curl -X POST http://localhost:8080/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "<project-name>",
    "source": "<source-url-from-bundle>",
    "ref": "<commit-sha-or-branch-from-bundle>",
    "mode": "dev"
  }'
```

The project is now reachable at `http://localhost:8080/<project-name>/`. You don't promote it locally — `dev` mode is fine for audit work, and avoids burning a TEE quote.

The container running the project is `tee-runtime-deno-dev`. You can `docker exec` into it, tail its logs, and modify files in its volume between requests. None of this is possible in production; the local replica gives it to you for free.

## What the trusted base provides

What the project handler can rely on, by contract:

- **`Deno.serve` is invoked once per request** with the request and a context object `{env, dataDir}`. `env` is the manifest's env-var block. `dataDir` is a writable per-project directory.
- **Filesystem reads from the project source dir** are isolated to that project (other projects on the same daemon can't read yours).
- **Network access** is unrestricted in dev, restricted by the docker proxy in attested.
- **Time** comes from whatever the handler chooses to fetch. There is no platform-supplied trusted clock. **If the project's claim depends on time, the project's own clock-source design is what you audit.**
- **Persistence** lives in `ctx.dataDir`. The volume survives runtime restarts but a CVM redeploy can replace it. Project authors who care about rollback resistance must say so in their design.

These are the building blocks. Anything beyond them is project code you must read.

## Reading the source

Open the source URL pinned in the bundle. Don't read top-to-bottom; read with a question.

The shape of the question, for any project:

1. **What is the trust claim?** Find it in the README or top-of-file comments. If it's not stated, that's the first audit finding.
2. **What invariants would have to hold for that claim to be true?** Express them as testable statements ("the key is not returned before `releaseTime`", "the receipt's prompt field equals the prompt actually sent to the model").
3. **What inputs and state can affect those invariants?** Trace from the handler signature outward: request body, env vars, `dataDir`, network calls, time sources.
4. **For each invariant, can the inputs or state break it?** Read the code paths that touch each one.

## Probing the local replica

Reading is necessary but not sufficient. Prepare adversarial inputs and run them against the local replica. Examples of probes that generalize:

- **Hostile inputs.** Send malformed or boundary-value requests. The handler should reject what it can't parse rather than accidentally treating it as the happy path.
- **Time manipulation.** If the project fetches time, redirect its time sources (DNS override, hosts file, transparent proxy). See whether the handler trusts a single source or quorum-checks.
- **Storage manipulation.** Stop the runtime, edit files in `ctx.dataDir`, restart. See whether the handler validates state on read or assumes it's intact.
- **State after restart.** Kill the runtime mid-handler. Restart. See whether half-completed state is recovered safely or treated as a fresh start.
- **Concurrent requests.** Issue requests simultaneously around an invariant boundary. See whether the check is atomic.

Each probe maps to an invariant. Document which invariant you tested, what you tried, and whether the project upheld it.

## Project-specific runbook: timelock

`timelock`'s trust claim: a sealed message's key is not returned until a release time the user specified.

The invariants you'd test:

- **Time gate.** No request, regardless of inputs, returns the key before `releaseTime`. Probe by sending the release request before, at, and after the boundary.
- **Trusted clock.** No single hostile time source can convince the handler the release time has passed. Probe by replacing one of `TIME_SOURCES` with a future-time mock.
- **Storage integrity.** A rollback of `ctx.dataDir` doesn't release a previously-sealed key on its own. Probe by capturing the data dir before sealing, then restoring it after release.
- **Idempotency.** Replaying a release request after a successful release doesn't reveal additional information or weaken the system.

A first-pass agent could implement each probe as a shell script under `audits/timelock/`. The output of each probe (request, response, latency, side-effects) is itself audit evidence.

## What an audit report should contain

The deliverable is a structured artifact the original requester (and their counterparties) can inspect.

- **Bundle reference.** The URL of the evidence bundle this audit is grounded in. Pin the commit SHA — the audit is only valid for that source.
- **Stated trust claim.** As you understood it, with a citation to where in the source or README you found it.
- **Invariants identified.** The list of statements you decided would have to hold for the claim to be true.
- **Probes attempted.** For each invariant, what inputs you tried and what the system did.
- **Findings.** Specific places in the source where the invariants hold, fail, or are ambiguous.
- **Out of scope.** What you didn't audit. The substrate, the TEE platform's own guarantees, the network outside the CVM, the project author's intent — all of these belong on this list explicitly.
- **Confidence.** A plain-language statement: "I am confident that …", "I could not rule out …", "the project author would need to clarify …".

Sign it (PGP, sigstore, whatever convention the requester uses) and post it where the requester can find it. The bundle plus a signed audit report is the actual artifact a counterparty acts on.

## How this fits the larger flow

A project author writes a small handler. They deploy it on a CVM and promote it. They publish the [evidence bundle](verify.md) URL. A relying party who needs the output to mean something forwards the URL — to themselves if they can read code, or to an auditor (human or agent) if they can't. The auditor follows this runbook, produces a report, and the relying party reads the report instead of the source.

The substrate's job is to make the auditor's job small and repeatable. The project's job is to be small enough that "read it and probe it" is genuinely possible. This page is the contract between those two halves.

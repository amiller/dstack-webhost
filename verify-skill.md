---
layout: default
title: Agent verifier skill
---

# Agent verifier skill

For an agent given a URL on a tee-daemon CVM, asked: *should the user trust this?* Produce a written verdict.

This is the lightweight, no-local-setup version. The deeper variant — set up a local replica, probe it, write findings against a project-specific runbook — lives in [audit.md](audit.html).

## Inputs

A URL of one of these shapes:

- `https://<cvm>/<project>/` — a specific app
- `https://<cvm>/` — the CVM's listing; pick one or all attested apps
- `https://<cvm>/_api/verification/<project>` — the verification bundle directly

You need outbound HTTP (the CVM is public; so is GitHub). For source review, the ability to dispatch a subagent.

## 1. Walk the substrate

```bash
CVM=https://<cvm>
NAME=<project>

# Listing — what's attested on this CVM
curl -s -H 'Accept: application/json' $CVM/

# Verification bundle — manifest, dstack quote, audit log
curl -s $CVM/_api/verification/$NAME
```

From the bundle, pull `project.source`, `project.commit_sha`, `project.tree_hash`, and the `audit` array.

**Source check.** The tree hash must match what GitHub reports for the pinned commit:

```bash
curl -s https://api.github.com/repos/<owner>/<repo>/commits/$COMMIT \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["commit"]["tree"]["sha"])'
```

Equal → running code = published source. Not equal → halt, report the mismatch, stop.

**Audit log.** Read it in order. The latest `promote` or `deploy` is the current attested state; its tree hash is the one you just verified. Earlier entries with different hashes can come from redeploys or platform hash-scheme changes — call them out in the verdict, don't fail on them.

**Quote.** The bundle includes a dstack quote. Walking it back to the on-chain CVM identity needs first-party dstack tooling that this skill does not wrap. State in the verdict: *quote present, not independently verified by this skill.*

## 2. Audit the source

The substrate check tells you the CVM runs code published at commit X. It does not tell you whether that code earns the trust claim it makes. That is per-project, and it is where most of the work is.

Dispatch a subagent. Prompt template:

> You are auditing a TEE-attested app for a relying party.
>
> Source: `<source URL>` at commit `<commit_sha>` (tree `<tree_hash>`).
>
> The app's claimed trust property: *<state the pitch — "encrypted message released only after deadline," "bot's signing key generated inside the TEE," etc. Pull from the app's UI or README>.*
>
> Read the source and answer:
> 1. What does the handler do? One paragraph — endpoints, state, crypto.
> 2. Does the trust claim hold against the code? Where could the operator (CVM admin) violate the property? Look for fallbacks to operator-controllable state, admin paths left in, time/randomness sources the operator can MITM, secrets stored in plaintext.
> 3. List notable weaknesses honestly. This is a personal demo; "uses local clock," "no quorum," "key in memory" are expected. Flag them.
> 4. Bottom line: would you trust it for a non-trivial use of the claim?
>
> Quote line numbers from the actual files. Read via the GitHub API for one-file projects; clone to `/tmp` for multi-file ones. Under 400 words.

## 3. Compose the verdict

Two parts.

**Substrate.** "Running code at `<project>` matches `<source>` commit `<short_sha>`. Audit log: N entries, latest `<action>` on `<date>`." Mention oddities (legacy hashes, quote not walked) without inflating them.

**Source.** The subagent's bottom line plus the most important caveats. The relying party should leave knowing what they can and cannot rely on.

## What this skill does not do

- Walk the dstack quote back to the on-chain CVM ID. Use dstack's verifier tools.
- Set up a local replica. See [audit.md](audit.html).
- Decide whether the trust claim is well-posed. That is a design review, not an audit.

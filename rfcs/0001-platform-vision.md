# RFC: dstack-webhost -- A Verifiable TEE Web Hosting Platform

**Status**: Draft  
**Repo**: https://github.com/amiller/dstack-webhost

## Problem

Deploying a web app inside a Phala dstack CVM is technically possible today,
but the experience is raw -- you write a docker-compose, push it through the
phala CLI, and you're on your own. There's no framework for:

- Hosting multiple apps on one CVM with per-app isolation
- Promoting apps from development to TEE-attested production
- Giving end users a way to verify what code is actually running
- Maintaining an audit trail that's meaningful and inspectable

## Vision

dstack-webhost is a hosting platform that lives inside a Phala dstack CVM and
makes it easy to deploy, promote, and verify web applications with TEE
attestation guarantees. It has three interacting concerns:

1. **Hosting** -- two layers of deployment granularity
2. **Trust modes** -- dev and attested, with promotion between them
3. **Verification** -- end-to-end trust chain from smart contract to running code


## 1. Hosting: Two Layers

### Layer 1: Docker Service Hosting

Full docker-compose subnetworks running inside the CVM. You bring a
docker-compose.yaml and the daemon manages it as an attested (or dev) service.
Use this for databases, multi-container apps, anything that needs its own
network topology.

### Layer 2: Lightweight App Hosting

Single-function apps in Deno, Node, Python, or static files. You point at a
git repo and an entry file, and the daemon deploys it onto a shared runtime
container. No Docker knowledge needed. Think of it like Cloudflare Workers
but the workers are TEE-attested.

These compose naturally -- a Layer 2 app can talk to a Layer 1 database
service on the same CVM's internal network.


## 2. Trust Modes: Dev and Attested

Every project exists in one of two modes:

### Dev Mode
- Deploy anything, iterate fast
- No attestation, no audit trail
- Shared dev Docker proxy (less restricted)
- Good for testing, staging, experiments

### Attested Mode
- TEE-isolated containers on a separate network
- Docker socket proxy enforces policy -- attested containers cannot be
  tampered with from dev mode or outside the TEE
- Audit log for all management actions (deploy, update, config change)
- dstack attestation bound to deployed source code
- The TEE guarantees the daemon's policy enforcement is itself trustworthy

### Promotion

A deliberate action: `POST /_api/projects/<name>/promote`

- Re-deploys the project on the attested network
- Binds it to the dstack socket for attestation
- Enables audit logging from that point forward
- The promotion event itself is the first audited entry
- Source code hash is recorded and becomes part of the attestation claim

This is intentionally not automatic. Promotion is a trust claim, and it
should be a conscious step -- like merging to main, but for trust.


## 3. Verification: The Trust Chain

This is the part that makes this project more than just a deployment tool.
The whole point of running in a TEE is that someone else can verify what's
running. The trust chain looks like:

```
Base smart contract (CVM app ID)
  └── CVM attestation (dstack quote, includes TCB, boot measurements)
        └── tee-daemon code (verified via repo commit hash in quote)
              └── Per-project attestation
                    ├── Source code hash (git tree hash from deploy)
                    ├── Deployment manifest (runtime, entry, env)
                    └── Audit log (all management actions since promotion)
```

### For Developers: Workflow Skills

The repo should provide guidance (and eventually agent skills) for:

1. **Local dev**: Write your app in TypeScript, test locally
2. **Staging deploy**: `POST /_api/projects` with your git repo, see it live
   on the CVM in dev mode
3. **Promote**: When you're happy, promote to attested. The daemon records
   your source hash and starts the audit trail
4. **Share verification**: Your users can follow the verifier flow below

### For End Users: Verification Skills

Someone visiting `https://your-cvm.dstack.phala.network/your-app/` should
be able to answer:

- "What code is running right now?" -> Check the project's attested source
  hash against the GitHub repo
- "Is the TEE actually enforcing anything?" -> Verify the CVM's dstack quote
  against the Base smart contract
- "Has anything changed since I last checked?" -> Inspect the audit log
- "Can I trust this app?" -> Walk the full chain from on-chain CVM to
  source code to audit trail

The repo should provide:
- **README guidance** for manual verification
- **Agent skill** for automated verification (given a URL, walk the chain
  and report what's running)
- **Browser-friendly verification page** that each attested app can serve
  at `/.well-known/tee-attestation` or similar


## Status (2026-04-26)

The original "Next Steps" list is mostly landed:

- Network separation (`tee-apps-dev` and `tee-apps-attested`) — done.
- `mode: dev|attested` on projects with promotion API — done.
- Per-project audit log, attested-only — done.
- Public read-only verifier endpoints (RFC 0015) so a relying party doesn't need the admin token — done.
- Source hash recorded as the git tree SHA so it can be checked against GitHub — done.
- [Verifier page](../verify.md) and [audit guide](../audit.md) on the docs site — done.

The verifier page and audit guide together replace what RFC 0001 originally called the "verification page" and "agent skill." The substrate is small enough that a project audit reduces to reading the project's own handler against a known runtime contract; the audit guide is the runbook for that.

What's still off:

- Custom domains. Apps live at `<cvm>/<project-name>/`.
- CI/CD-triggered redeploys. Available via API; no first-party hook.
- Multi-CVM federation. Out of scope.
- Pre-vetted packet libraries (community-published handler templates).
- Auditor-side tooling that scripts the runbook against a verifier-bundle URL.


## Non-Goals (for now)

- Multi-CVM federation (one CVM, one daemon)
- User authentication for the management API (token-based is fine)
- Automatic CI/CD promotion (promotion is manual by design)
- Custom domains (using dstack's built-in URL scheme for now)

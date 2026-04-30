---
layout: default
---

<div style="display:flex;align-items:center;gap:18px;margin:0.5em 0 1.5em">
  <img src="assets/logo.svg" alt="" width="56" height="56" style="flex:none"/>
  <span>A personal Vercel for attestable web apps. One Phala dstack CVM hosts many little apps you wrote yourself; each one ships with evidence of what code actually ran.</span>
</div>

<p style="text-align:center;margin:1.5em 0">
  <img src="assets/diagram.svg" alt="A CVM hosts many apps; one app's attestation goes to a recipient who reads the source code." style="max-width:100%;height:auto"/>
</p>

Lambda, Vercel, and Cloudflare Workers host code cheaply and call it on demand. None of them tell the consumer of the output what code ran. For most apps that's fine. For receipts, credentials, sealed-time releases, or evidence in disputes, it's the whole point.

A function running inside a Phala dstack TEE can answer the question. The platform produces a hardware-rooted attestation that binds the running code's measurements into the output. dstack-webhost wraps that into a normal dev loop: push to a git repo, deploy to your CVM, promote to attested when you're ready, share the URL. Whoever you share with sees the source on GitHub and the running code as the same thing.

## Things you might build

- **Prompt receipts.** Call a model provider through a TEE function and emit the response together with a signed record of the exact prompt and response.
- **Timelock encryption.** Hold an encrypted message and only release the key after a deadline. The TEE seals the key; a quorum of clocks gates the release. A working demo runs on hermes-staging.
- **Multi-tenant isolation, verifiable.** Two tenants of the same CVM, sandboxed from each other under `sysbox-runc`. A small probe app exposes its own kernel-namespace view so a relying party can corroborate the substrate's runtime claim from inside one tenant — covered on [the isolation probe page](isolation-probe.md).
- **ZK-TLS credentials.** Run an attested TLS session against a website where you have an account; emit a sealed claim the recipient can verify without seeing your password.
- **Document gateways.** Take a document, run it through "standard rental-agreement-template-v3", return a signed parse. The packet's identity is its source hash, so the recipient trusts the published version rather than reading every byte of your custom code.

## Two modes per project

**Dev** is for iteration. Push code, test, change it, throw it away. No audit log, no public verifier; trust is whatever you'd get on Vercel.

**Attested** is the trust claim. The daemon records the source hash, opens an audit log, binds the hash into the TEE quote, and exposes the verifier endpoints to anonymous callers. Promotion is deliberate — like cutting a release.

A single CVM holds as many of each as you like. Most apps stay private. You share the URL of an attested project when someone needs to verify it.

## Why audits stay small

Every project on a tee-daemon CVM rides the same daemon, the same shared runtime, and the same verifier surface. An auditor learns that substrate once — it's [public source](https://github.com/amiller/dstack-webhost) — and reuses the understanding across every project they review on it. A specific project's audit reduces to "given this known substrate, does this small handler hold the invariant it claims?" The platform's payoff is keeping each per-project audit small.

## See it live

A working instance is at [hermes-staging](https://915c8197b20b831c52cf97a9fb7e2e104cdc6ae8-8080.dstack-pha-prod7.phala.network/). The CVM serves its own list of attested apps with [verifier](verify.md) links beside each one.

## Status

Pre-v1. The hosting flow works across Deno, Node, Bun, Python, static, and custom Dockerfiles. Attested promotion, the audit log, and public verifier endpoints landed in late April. Next milestones live in the [RFC log](rfcs/).

## Reading and discussion

The substance lives in the RFC log. Issues there track the work in flight.

- [Platform vision (RFC 0001)](rfcs/0001-platform-vision.md) — the original framing, with a current-state update
- [RFC log](rfcs/) — design notes, dated and status-tagged
- [Developer guide](DEVELOPER_GUIDE.md) — deploying a project
- [Audit guide](audit.md) — auditing a deployed project, agent-runnable
- [Verifier skill](verify-skill.md) — for an agent handed just a URL: walk the chain, produce a verdict
- [Isolation probe](isolation-probe.md) — a sample tenant that corroborates the substrate's OCI-runtime claim from inside one container
- [GitHub repo](https://github.com/amiller/dstack-webhost)

Open a [GitHub issue](https://github.com/amiller/dstack-webhost/issues). Most of the design is still up for revision.

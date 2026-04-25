---
layout: default
title: tee-daemon
---

# tee-daemon

Verifiable TEE web hosting for Phala dstack CVMs. Deploy a multi-language web app inside a TEE with attestation guarantees so anyone can verify what code is running.

The most interesting use case: an agent writes a server, deploys it here, and ships the URL together with a built-in proof chain. Whoever you send it to can walk smart contract → CVM attestation → daemon code → per-project source hash → audit log without trusting you.

## Status

Pre-v1. The dev-mode hosting flow works today across Deno, Node, Bun, Python, static, and custom Dockerfiles. The attested-promotion and end-to-end verification chain are designed but not yet exercised in practice — that gap is the main thing we're working on.

## What this site is

A working notebook for the project's design. Most of the substance is in the RFC log; issues track the work falling out of those RFCs. Open discussions and PRs welcome.

- [Platform vision (RFC 0001)](rfcs/0001-platform-vision.md)
- [RFC log](rfcs/)
- [Known issues](ISSUES.md)
- [Developer guide](DEVELOPER_GUIDE.md)
- [GitHub repo](https://github.com/amiller/dstack-webhost)

## Discussion

Open a [GitHub issue](https://github.com/amiller/dstack-webhost/issues) — most of the design is still up for revision.

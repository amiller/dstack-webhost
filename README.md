# dstack-webhost

A personal Vercel for attestable web apps. Hosts many projects on a single Phala dstack CVM; each can be promoted to **attested** mode where the source hash binds into the TEE quote and the verifier endpoints become publicly readable.

The daemon (`tee-daemon`) is the substrate: it does the ingress, the shared language runtime, the audit log, and the verifier surface. Project authors write small handlers; this code does everything else, once, for every project on a CVM.

Vocabulary, once, up front — three layers are easy to blur. The **host owner** runs the CVM and chooses what runs on it. An **app** (a `project` in the code) is a deployed unit with its own container, source pin, and quote. A **user** (an *agent* in the Solid sense) is a person who delegates to many apps. The substrate isolates apps from each other; user-from-user isolation is enforced inside the apps' own credential core (see oauth3-server), not by this daemon — the [isolation probe](isolation-probe.md) says which boundary each runtime mechanism does and does not enforce.

## Quick start

```bash
# Local dev
docker compose up

# Deploy to a Phala CVM
phala deploy --cvm-name my-daemon -c docker-compose.yaml -e .env
```

## Documentation

- **Platform overview, vision, RFC log:** https://amiller.github.io/dstack-webhost
- **Deploying a project:** [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)
- **Auditing a deployed project:** https://amiller.github.io/dstack-webhost/audit.html
- **Verifying a deployed project (relying-party):** https://amiller.github.io/dstack-webhost/verify.html

## Source layout

- `proxy/ingress.py` — request routing, auth gate, public verifier endpoints.
- `proxy/runtimes.py` — language-runtime container management; the Deno router that loads project handlers.
- `proxy/deploy.py` — git-clone path, source-hash recording, project promotion.
- `proxy/templates/index.html` — the default viewer page each CVM serves at `/`.
- `rfcs/` — design discussion. Numbered, dated, status-tagged.

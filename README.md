# tee-daemon

A TEE app hosting platform for Phala dstack CVMs. Deploys and manages
multi-language web apps inside a TEE with attestation guarantees.

## What it does

- **Ingress proxy** on port 8080 routes traffic to deployed apps by name
- **Multi-runtime support**: Deno, Bun, Node.js, Python, static files, custom Dockerfiles
- **Docker socket proxy** lets tenant containers use Docker safely (tracked + audited)
- **dstack socket proxy** for TEE attestation (GetQuote, GetKey, etc.)
- **Management API** at `/_api/...` for deploy/teardown/redeploy from git repos
- **Attestation** per-project via dstack KMS

## Quick start

```bash
# Local dev
docker compose up

# Deploy to Phala CVM
phala deploy --cvm-name my-daemon -c docker-compose.yaml -e .env
```

## API

```
GET  /_api/projects              # list all projects
POST /_api/projects              # deploy (JSON body)
GET  /_api/projects/<name>       # status
DELETE /_api/projects/<name>     # teardown
POST /_api/projects/<name>/redeploy  # redeploy from git
GET  /_api/attest/<name>         # get attestation for project
GET  /_api/audit                 # audit log
```

## Deploying an app

```bash
curl -X POST http://localhost:8080/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-app",
    "runtime": "deno",
    "entry": "server.ts",
    "source": "https://github.com/user/repo",
    "ref": "main",
    "attested": true
  }'
```

Then access at `http://localhost:8080/my-app/`.

## Architecture

```
Port 8080 (Ingress)
  ├── /_api/...          → Management API
  ├── /<project>/...     → Runtime containers (deno/node/python)
  │                          via shared per-language routers
  └── /<project>/...     → Static file serving

Unix sockets (for sibling containers):
  ├── /var/run/proxy/docker.sock  → Docker API proxy (audited)
  └── /var/run/proxy/dstack.sock  → dstack TEE API proxy
```

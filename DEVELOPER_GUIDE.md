# Developer Guide: From Local TypeScript to Attested Production

This guide walks you through deploying a web application from local development to a TEE-attested production environment using dstack-webhost.

## Prerequisites

- Access to a Phala dstack CVM running tee-daemon
- A GitHub/GitLab repository with your application code
- `curl` or similar HTTP client for API calls
- A TEE daemon token (set via `TEE_DAEMON_TOKEN` environment variable)

---

## 1. Local Development

### Supported Runtimes

tee-daemon supports multiple runtimes out of the box:

| Runtime | Default Entry | Default Port | Notes |
|---------|--------------|--------------|-------|
| `deno`  | `server.ts`  | 3000         | Requires no build step |
| `bun`   | `index.ts`   | 3000         | Fast TypeScript runtime |
| `node`  | `index.js`   | 3000         | Supports `package.json` |
| `python`| `app.py`     | 8000         | Supports `requirements.txt` |
| `static`| `.`          | 8080         | Static HTML/CSS/JS files |
| `dockerfile` | `Dockerfile` | 8080 | Custom container builds |

### Example: Deno TypeScript App

Create a simple Deno server:

```typescript
// server.ts
import { serve } from "https://deno.land/std@0.208.0/http/server.ts";

const handler = async (req: Request) => {
  const url = new URL(req.url);
  
  if (url.pathname === "/api/hello") {
    return new Response(JSON.stringify({ message: "Hello from TEE!" }), {
      headers: { "Content-Type": "application/json" },
    });
  }
  
  return new Response("Welcome to my TEE app!", {
    headers: { "Content-Type": "text/plain" },
  });
};

serve(handler, { port: 3000 });
```

Test locally:

```bash
deno run --allow-net server.ts
```

Visit `http://localhost:3000` to verify.

### Example: Node.js Express App

```javascript
// package.json
{
  "name": "my-tee-app",
  "version": "1.0.0",
  "dependencies": {
    "express": "^4.18.0"
  }
}
```

```javascript
// index.js
const express = require('express');
const app = express();
const PORT = 3000;

app.get('/', (req, res) => {
  res.json({ message: 'Hello from TEE!' });
});

app.get('/api/status', (req, res) => {
  res.json({ status: 'running', mode: 'attested' });
});

app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});
```

### Example: Python Flask App

```python
# app.py
from flask import Flask, jsonify

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"message": "Hello from TEE!"})

@app.route('/api/health')
def health():
    return jsonify({"status": "healthy"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
```

With a `requirements.txt`:

```
flask==3.0.0
```

---

## 2. Staging Deployment (Dev Mode)

### Option A: Auto-Detection (Simplest)

If your repo has a detectable entry file, tee-daemon can auto-detect the runtime:

```bash
curl -X POST https://<your-cvm>.dstack.phala.network/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-app",
    "source": "https://github.com/yourusername/your-repo",
    "ref": "main"
  }'
```

The daemon will detect:
- `server.ts` → Deno
- `index.ts` → Bun
- `index.js` → Node.js
- `app.py` → Python
- `index.html` → Static

### Option B: Explicit Configuration

For more control, specify the runtime and entry:

```bash
curl -X POST https://<your-cvm>.dstack.phala.network/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-app",
    "runtime": "deno",
    "entry": "server.ts",
    "port": 3000,
    "source": "https://github.com/yourusername/your-repo",
    "ref": "main",
    "mode": "dev"
  }'
```

### Environment Variables

Pass environment variables to your app:

```bash
curl -X POST https://<your-cvm>.dstack.phala.network/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-app",
    "runtime": "node",
    "entry": "index.js",
    "source": "https://github.com/yourusername/your-repo",
    "mode": "dev",
    "env": {
      "API_KEY": "your-secret-key",
      "DEBUG": "true"
    }
  }'
```

### Access Your App

Once deployed, access your app at:

```
https://<your-cvm>.dstack.phala.network/my-app/
```

### Custom Port Binding (Optional)

For dedicated port access (bypassing path routing):

```bash
curl -X POST https://<your-cvm>.dstack.phala.network/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-app",
    "runtime": "node",
    "entry": "index.js",
    "source": "https://github.com/yourusername/your-repo",
    "listen": {
      "port": 9000,
      "protocol": "http"
    }
  }'
```

Access directly at: `https://<your-cvm>.dstack.phala.network:9000/`

**Note:** Port 8080 is reserved for path-based routing. Other ports require unique assignment.

### Project Configuration File

For more complex projects, add a `project.json` to your repo root:

```json
{
  "runtime": "node",
  "entry": "index.js",
  "port": 3000,
  "mode": "dev",
  "env": {
    "NODE_ENV": "production"
  },
  "listen": {
    "port": 8080,
    "protocol": "http"
  }
}
```

### Verify Deployment

Check project status:

```bash
curl https://<your-cvm>.dstack.phala.network/_api/projects/my-app \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "name": "my-app",
  "runtime": "node",
  "entry": "index.js",
  "port": 3000,
  "mode": "dev",
  "source": "https://github.com/yourusername/your-repo",
  "ref": "main",
  "commit_sha": "abc123...",
  "tree_hash": "def456...",
  "deployed_at": "2024-01-15T10:30:00Z"
}
```

### List All Projects

```bash
curl https://<your-cvm>.dstack.phala.network/_api/projects \
  -H "Authorization: Bearer $TOKEN"
```

---

## 3. Redeploy

To update your app with the latest changes:

```bash
curl -X POST https://<your-cvm>.dstack.phala.network/_api/projects/my-app/redeploy \
  -H "Authorization: Bearer $TOKEN"
```

The daemon will pull the latest commit from your git repo and redeploy.

---

## 4. Promote to Attested Mode

When you're ready to move to production with TEE attestation guarantees:

### Understanding Dev vs Attested Mode

| Feature | Dev Mode | Attested Mode |
|---------|----------|---------------|
| Network | `tee-apps-dev` | `tee-apps-attested` |
| Docker Proxy | Less restricted | Enforces TEE policy |
| Audit Log | Disabled | Enabled |
| Attestation | No | Yes (via dstack) |
| Use Case | Testing, staging | Production |

### Promotion Process

```bash
curl -X POST https://<your-cvm>.dstack.phala.network/_api/projects/my-app/promote \
  -H "Authorization: Bearer $TOKEN"
```

What happens during promotion:

1. Project mode changes from `dev` to `attested`
2. App is redeployed on the `tee-apps-attested` network
3. Docker proxy enforces stricter security policies
4. Audit logging is enabled
5. Source code hash is recorded in the attestation claim
6. First audit entry is the promotion event itself

### Verification After Promotion

The promotion is recorded in the audit log:

```bash
curl https://<your-cvm>.dstack.phala.network/_api/projects/my-app/audit \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "project": "my-app",
  "mode": "attested",
  "entries": [
    {
      "timestamp": 1705318200.123,
      "action": "promote",
      "image_digest": "sha256:...",
      "detail": "{\"name\":\"my-app\",\"from_mode\":\"dev\",\"to_mode\":\"attested\",\"commit\":\"abc123...\",\"tree_hash\":\"def456...\"}"
    }
  ]
}
```

---

## 5. Verification

### For Developers: Verify Your Deployment

Get the attestation data for your project:

```bash
curl https://<your-cvm>.dstack.phala.network/_api/verification/my-app
```

Response:

```json
{
  "project": {
    "name": "my-app",
    "runtime": "node",
    "mode": "attested",
    "source": "https://github.com/yourusername/your-repo",
    "commit_sha": "abc123...",
    "tree_hash": "def456...",
    "image_digest": "sha256:..."
  },
  "quote": {
    "path": "/tee-daemon/projects/my-app",
    "quote": "base64-encoded-dstack-quote...",
    "report": "base64-encoded-sgx-report..."
  },
  "audit": [...]
}
```

### For End Users: Browser Verification

Users can verify your app by visiting:

```
https://<your-cvm>.dstack.phala.network/my-app/.well-known/tee-attestation
```

This serves a verification page that displays:
- Project details (name, runtime, source)
- Source code hash (tree hash)
- Git commit SHA
- Docker image digest
- Audit log entries
- TEE attestation quote

### Manual Verification Steps

Users can manually verify the trust chain:

1. **Get Project Data**: Fetch `/_api/verification/<name>`
2. **Verify Source Hash**: Compare `tree_hash` with local git tree hash:
   ```bash
   git ls-tree -r HEAD | git hash-object --stdin
   ```
3. **Verify Commit**: Check that `commit_sha` matches the GitHub commit
4. **Verify Attestation**: Validate the dstack quote against the base CVM smart contract
5. **Check Audit Log**: Review audit entries for any unexpected changes

---

## 6. Management Operations

### Delete a Project

```bash
curl -X DELETE https://<your-cvm>.dstack.phala.network/_api/projects/my-app \
  -H "Authorization: Bearer $TOKEN"
```

### Get Routing Table

```bash
curl https://<your-cvm>.dstack.phala.network/_api/routes
```

Response:

```json
[
  {
    "host_port": 8080,
    "protocol": "http",
    "project": "(ingress)",
    "backend": "path-based routing"
  },
  {
    "host_port": 9000,
    "protocol": "http",
    "project": "my-app",
    "backend": "172.18.0.5:3000"
  }
]
```

---

## 7. Common Workflows

### Deploy and Promote in One Go

Skip dev mode for trusted code:

```bash
curl -X POST https://<your-cvm>.dstack.phala.network/_api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-app",
    "runtime": "deno",
    "entry": "server.ts",
    "source": "https://github.com/yourusername/your-repo",
    "mode": "attested"
  }'
```

### Staging Pipeline

1. Deploy to dev mode for testing
2. Test the app at `/my-app/`
3. When satisfied, promote to attested
4. Share the verification URL with users

### Multi-Environment Setup

Create separate projects for dev/staging/prod:

```bash
# Dev environment
curl -X POST .../projects \
  -d '{"name": "my-app-dev", "source": "...", "mode": "dev", "ref": "dev"}'

# Production environment
curl -X POST .../projects \
  -d '{"name": "my-app-prod", "source": "...", "mode": "attested", "ref": "main"}'
```

---

## 8. Troubleshooting

### Deployment Fails

- Check that your git repo is public or has proper authentication
- Verify the runtime is supported
- Ensure the entry file exists in your repo

### App Not Accessible

- Verify the project name in the URL
- Check that the runtime container is running
- Review the project status via the API

### Port Conflict

If you get a port conflict error:

```json
{"error": "Port conflict: project 'my-app' cannot bind to port 9000 because it is already in use by project 'other-app'"}
```

Either:
- Use a different port
- Delete the conflicting project first
- Use path-based routing on port 8080

### Verification Fails

- Ensure the project is in `attested` mode
- Check that dstack socket is available
- Verify the CVM has attestation enabled

---

## 9. Best Practices

### Security

1. **Never commit secrets**: Use environment variables for sensitive data
2. **Use attested mode for production**: Leverage TEE security guarantees
3. **Review audit logs**: Monitor for unexpected changes
4. **Verify source hashes**: Ensure deployed code matches your repository

### Development Workflow

1. **Develop locally**: Test your app before deploying
2. **Deploy to dev mode**: Verify behavior on the CVM
3. **Promote when ready**: Move to attested mode for production
4. **Share verification**: Give users the verification URL

### CI/CD Integration

```bash
#!/bin/bash
# deploy.sh

# Deploy to dev
curl -X POST "$CVM_URL/_api/projects" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"$APP_NAME\",\"runtime\":\"$RUNTIME\",\"entry\":\"$ENTRY\",\"source\":\"$REPO\",\"mode\":\"dev\",\"ref\":\"$BRANCH\"}"

# Run tests against dev instance
npm run test:staging

# If tests pass, promote to attested
curl -X POST "$CVM_URL/_api/projects/$APP_NAME/promote" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 10. Advanced Features

### Dockerfile Deployments

For apps needing custom containers:

```dockerfile
# Dockerfile
FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --production
COPY . .
CMD ["node", "index.js"]
```

Deploy:

```bash
curl -X POST .../projects \
  -d '{"name": "my-app", "runtime": "dockerfile", "source": "..."}'
```

### Static Site Deployment

```bash
curl -X POST .../projects \
  -d '{"name": "my-site", "runtime": "static", "source": "https://github.com/user/my-website"}'
```

### TCP Services (Non-HTTP)

For raw TCP services like databases or custom protocols:

```bash
curl -X POST .../projects \
  -d '{
    "name": "my-tcp-service",
    "runtime": "node",
    "source": "...",
    "listen": {
      "port": 5432,
      "protocol": "tcp"
    }
  }'
```

---

## API Reference Summary

### Projects

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/_api/projects` | List all projects |
| POST | `/_api/projects` | Deploy new project |
| GET | `/_api/projects/<name>` | Get project status |
| DELETE | `/_api/projects/<name>` | Delete project |
| POST | `/_api/projects/<name>/redeploy` | Redeploy from git |
| POST | `/_api/projects/<name>/promote` | Promote to attested |
| GET | `/_api/projects/<name>/audit` | Get audit log |

### Verification

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/_api/attest/<name>` | Get dstack attestation |
| GET | `/_api/verification/<name>` | Get full trust chain |
| GET | `/my-app/.well-known/tee-attestation` | Browser verification page |

### Routing

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/_api/routes` | Get routing table |

---

## Support

- **Issues**: Report bugs on the GitHub repository
- **RFCs**: See `/rfcs/` for design documentation
- **Source Code**: `/proxy/` for daemon implementation

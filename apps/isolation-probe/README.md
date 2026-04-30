# isolation-probe

Tiny Layer-1 (image runtime) tenant for dstack-webhost. Exposes the
container's own `/proc` namespace data so a relying party can corroborate
the substrate's [`/_api/substrate`](../../proxy/ingress.py) runtime claim
with evidence the substrate cannot fabricate.

Lives next to the [tunnel demo](../tunnel/) as a sibling small-app for
the docs site.

## Build & push

```bash
docker build -t ghcr.io/amiller/tee-isolation-probe:latest \
             -t ghcr.io/amiller/tee-isolation-probe:v1 .
docker push ghcr.io/amiller/tee-isolation-probe:v1
docker push ghcr.io/amiller/tee-isolation-probe:latest
docker inspect ghcr.io/amiller/tee-isolation-probe:v1 \
  --format '{{index .RepoDigests 0}}'
```

## Deploy as a tenant

Pin the digest from the previous command, then POST a manifest:

```bash
curl -X POST https://<cvm>/_api/projects \
  -H "Authorization: Bearer $TEE_DAEMON_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "probe",
    "runtime": "image",
    "image": "ghcr.io/amiller/tee-isolation-probe@sha256:...",
    "image_port": 8000
  }'
```

Visit `https://<cvm>/probe/`. The page fetches `/_api/substrate` and the
tenant's own `/api/probe` endpoint, then renders a verdict comparing the
two.

## What a relying party sees

1. **Substrate claim** — JSON from `/_api/substrate`, including
   `effective_runtime` (e.g. `sysbox-runc` or `runc`).
2. **Tenant evidence** — JSON from the tenant's own kernel-namespace
   view: `uid_map`, `gid_map`, `user_ns`, `pid_ns`, `mount_ns`, `cgroup`.
3. **Verdict** — checks whether the substrate's claimed runtime is
   consistent with the tenant's `uid_map`. A shifted `uid_map`
   (e.g. `0 296608 65536`) is the signature of `sysbox-runc`'s
   automatic user-namespace remap; the trivial map (`0 0 4294967295`)
   means default `runc`.

A malicious substrate could lie in `/_api/substrate`, but cannot forge
the tenant's `/proc/self/uid_map`. That's the corroboration loop the
probe demonstrates.

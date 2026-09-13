# RFC 0023: Staging CVM for Autonomous Daemon Development

## Summary
Stand up a second, always-on CVM on pha-prod as the deploy target for autonomous (Paseo)
daemon development, separate from the load-bearing `hermes-staging` CVM. The Paseo loop builds
and deploys candidate daemon images to *staging*; a human tests against it; promotion to the
production CVM stays a laptop-only gated step (merge the PR to `origin/main`, then
`ship-fix.sh prod`). The capability to break production is held only by the laptop.

## Problem
There is no environment where an autonomous loop can deploy and exercise a candidate daemon
build without risking production.

- **"hermes-staging" is production.** It is the single daemon CVM and it is load-bearing: 16
  live projects (oauth3, otter, tinycloud, listen, timelock) plus the hermes matrix bot.
  `ship-fix.sh` deploys the platform image straight onto it (`phala deploy --cvm-id
  hermes-staging`). Shipping any daemon change today upgrades the live box.
- **The only autonomous gate is local.** The Paseo loop's execution-grounded check is
  `test_daemon.py` on zed — real docker, but **no TEE and no real fleet**. Anything that only
  manifests on a real CVM (`recover_all` across a reboot, the dstack socket wiring, ingress
  through the gateway, the attestation endpoints) is unverified until it reaches production.
- **No credential to hand an autonomous process.** The owner token is admin-everywhere (issue
  #18). There is no separate, lower-blast-radius credential to give the loop.

## Files / infra to modify
- `ship-fix.sh` — parameterize the deploy target. Today it hardcodes `hermes-staging`, one
  compose, one env. Change to `ship-fix.sh staging|prod`, selecting `{cvm-id, compose, env,
  token}` per target. The **prod** target + token live only on the laptop.
- A **staging** compose + env, separate from `hermes-agent/docker-compose.hermes-prod.yaml`
  (the hermes-staging CVM's compose — renamed to say what it is: prod): its own
  `TEE_DAEMON_TOKEN`, its own external named volumes — **not** `hermes_data`.
- The deploy-capability ladder (memory: `zed-remote-dev`) splits by environment: zed/Paseo gets
  rung-4-for-*staging* (ship daemon images to the staging CVM); rung-4-for-*prod* stays
  laptop-only.

## Implementation
1. **Provision the staging CVM** on pha-prod (same cluster as today) with its own `cvm-id`
   (e.g. `webhost-staging`), its own owner token, and its own external named volumes. It hosts
   only test projects the loop deploys — no co-tenant production apps.
2. **Parameterize `ship-fix.sh` by target.** `staging` → the staging cvm-id/compose/env/token;
   `prod` → the existing `hermes-staging` wiring. The prod target is absent from the loop's
   environment, so the loop *cannot* deploy to prod even by mistake.
3. **Autonomous deploy to staging.** After the local suite passes, the loop builds → pushes a
   staging-tagged image to ghcr → `phala deploy --cvm-id webhost-staging --wait`. This is the
   step the loop owns end to end.
4. **Human test step.** The operator exercises the candidate against the staging URL — the real
   CVM behaviors local docker can't show (recover, gateway ingress, and the attestation surface
   to the extent pha-prod exposes it; see Out of Scope on chain_id 0).
5. **Promote (laptop-gated).** Merge the PR to `origin/main` (existing path), then from the
   laptop run `ship-fix.sh prod`, which upgrades the production CVM. This is the only step that
   touches production, and it requires a human plus the laptop-held token.
6. **Hygiene for an always-on box (accepted drift).** The loop periodically redeploys staging
   from `origin/main` HEAD so the running image tracks origin, and tears down stale `test-*`
   projects between cycles. This is the lightweight anti-drift habit; full
   reproducible-from-pin restore is RFC 0017's job, not a prerequisite here.

## Testing & Validation Requirements
- A candidate daemon image deployed to staging reproduces the `test_daemon.py` behaviors
  against a real CVM, and `recover_all` brings the fleet back after a staging reboot.
- An autonomous deploy to staging **cannot** reach the production CVM: separate token + cvm-id,
  prod target not present in the loop's environment.
- `ship-fix.sh staging` and `ship-fix.sh prod` hit distinct CVMs with distinct tokens; a
  staging run never mutates `hermes_data` or any production project.
- Promotion: a PR merge followed by `ship-fix.sh prod` upgrades production and leaves staging
  untouched.

## Out of Scope
- **base-prod / on-chain attestation staging.** We chose pha-prod (chain_id 0 / no KMS), so the
  attestation path on staging is a placeholder — revisit if verify/evidence work (RFC 0020 /
  0021) needs a real on-chain backend to test against.
- **Ephemeral / reproducible-from-origin staging.** We chose an always-on pet; the
  pinned-restore anti-drift mechanism is RFC 0017.
- **General scoped tokens** (issue #18). This RFC needs only one separate staging token, not a
  full scoping model.
- **Multi-CVM federation / HA** (RFC 0001 non-goal; RFC 0019 covers the managed multi-tenant
  story).

---

## Roles as deployed (#59): webhost-staging vs hermes-staging
Two dstack CVMs look redundant but are **different layers**. `webhost-staging` is the
autonomous daemon-image test box where the Paseo loop rebuilds and verifies the tee-daemon
itself. `hermes-staging` is the load-bearing production/app host for the ~17 non-promoted
apps. Daemon-image development runs only on webhost-staging, because rebuilding the daemon
restarts it — on hermes-staging that would take down its live apps.

### webhost-staging — the autonomous daemon-image test CVM
Where the Paseo loop rebuilds and tests the **tee-daemon itself**: each `staging-NN` branch
is built, tested and deployed here (`ship-fix.sh staging`). Daemon-code issues depend on
this box — #57 (RFC 0017 export/import) and #58 (RFC 0026 scoped debug sessions) are daemon
code, so they build and verify here. webhost-staging is therefore **not** retire-able.

Daemon-image development **cannot** run on hermes-staging: rebuilding the daemon restarts
it and would take down its ~17 live apps.

### hermes-staging — the app host (misnamed; it is production, not staging)
Runs the ~17 non-promoted apps (otterscope, listen, router-dashboard, vault, tinycloud, …).
App-level development can happen here; daemon-image development cannot.

### Ship flow
Paseo loop → `staging-NN` branch → PR (built and tested on webhost-staging). Promotion to
production is a **laptop-only gated step**: review and merge the PR to `origin/main`, then
run `ship-fix.sh prod` — the prod target and `TEE_DAEMON_TOKEN` live only on the laptop.

`ship-fix.sh` keeps the targets visibly distinct:

| target | CVM | compose | env |
|---|---|---|---|
| `staging` | `webhost-staging` | `docker-compose.webhost-staging.yaml` | `.env.webhost-staging` |
| `prod` | `hermes-staging` | `docker-compose.hermes-prod.yaml` | `.env.hermes-prod` |
| `pod` | `oauth3-prod7` | `docker-compose.prod7.yaml` | `.env.prod9` |

(The `pod` target — pod.dstack on Base KMS — additionally requires `PRIVATE_KEY` in the
environment; `staging` and `prod` do not.)

### Deferred (mechanics, not principle): migrating apps onto the prod base CVM
Bulk-migrating the ~17 apps onto the prod base CVM (oauth3-prod7 / pod.dstack) is deferred,
not rejected — pod.dstack is a general daemon meant to host dev and attested apps (mode is
per-project, #16). Prerequisites:

- `DAEMON_CONTAINER_RUNTIME=runc` on the prod base — the prod OS has no runsc
- a resize of the target CVM
- source pulls: 7 apps' sources exist only on the hermes-staging daemon disk
- per-project secrets

### Tidy-up
Retire-able: the legacy Gen-1 enclave `oauth3-proxy-staging` (prod9) and the duplicate
`shared-key-demo-2`. Longer-term: rename hermes-staging to end the staging/production
confusion.

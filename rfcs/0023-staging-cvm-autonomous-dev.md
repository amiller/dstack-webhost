# RFC 0023: Staging CVM Roles — webhost-staging vs hermes-staging

## Summary
Two dstack CVMs look redundant but are **different layers**. `webhost-staging` is the
autonomous daemon-image test box where the Paseo loop rebuilds and verifies the tee-daemon
itself. `hermes-staging` is the load-bearing production/app host for the ~17 non-promoted
apps. Daemon-image development runs only on webhost-staging, because rebuilding the daemon
restarts it — on hermes-staging that would take down its live apps.

## webhost-staging — the autonomous daemon-image test CVM
Where the Paseo loop rebuilds and tests the **tee-daemon itself**: each `staging-NN` branch
is built, tested and deployed here (`ship-fix.sh staging`). Daemon-code issues depend on
this box — #57 (RFC 0017 export/import) and #58 (RFC 0026 scoped debug sessions) are daemon
code, so they build and verify here. webhost-staging is therefore **not** retire-able.

Daemon-image development **cannot** run on hermes-staging: rebuilding the daemon restarts
it and would take down its ~17 live apps.

## hermes-staging — the app host (misnamed; it is production, not staging)
Runs the ~17 non-promoted apps (otterscope, listen, router-dashboard, vault, tinycloud, …).
App-level development can happen here; daemon-image development cannot.

## Ship flow
Paseo loop → `staging-NN` branch → PR (built and tested on webhost-staging). Promotion to
production is a **laptop-only gated step**: review and merge the PR to `origin/main`, then
run `ship-fix.sh prod` — the prod target and `TEE_DAEMON_TOKEN` live only on the laptop.

`ship-fix.sh` keeps the two targets visibly distinct:

| target | CVM | compose | env |
|---|---|---|---|
| `staging` | `webhost-staging` | `docker-compose.webhost-staging.yaml` | `.env.webhost-staging` |
| `prod` | `hermes-staging` | `docker-compose.hermes-prod.yaml` | `.env.hermes-prod` |

## Deferred (mechanics, not principle): migrating apps onto the prod base CVM
Bulk-migrating the ~17 apps onto the prod base CVM (oauth3-prod7 / pod.dstack) is deferred,
not rejected — pod.dstack is a general daemon meant to host dev and attested apps (mode is
per-project, #16). Prerequisites:

- `DAEMON_CONTAINER_RUNTIME=runc` on the prod base — the prod OS has no runsc
- a resize of the target CVM
- source pulls: 7 apps' sources exist only on the hermes-staging daemon disk
- per-project secrets

## Tidy-up
Retire-able: the legacy Gen-1 enclave `oauth3-proxy-staging` (prod9) and the duplicate
`shared-key-demo-2`. Longer-term: rename hermes-staging to end the staging/production
confusion.

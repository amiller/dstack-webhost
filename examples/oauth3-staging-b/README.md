# staging-b oauth3 core

This deploys the same `oauth3-server/server` tree as the staging `oauth3` project under
`/oauth3b`. The daemon gives each isolated project its own persistent data volume and derives
`SEAL_KEY` from the project-scoped dstack path `/tee-daemon/projects/oauth3b/seal`; no key is
committed or copied through the manifest.

From a checkout with the staging credentials loaded:

```sh
set -a; source ~/.tee-daemon-staging.env; set +a
OAUTH3_SERVER_DIR=~/projects/oauth3-server ./examples/oauth3-staging-b/deploy.sh
```

The manifest pins the currently deployed staging core's `GIT_SHA`; update it when staging
`/oauth3` moves to a new core version. Verify both routes and the returned image digest before
using `oauth3b` as a migration destination.

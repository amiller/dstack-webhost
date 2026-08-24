# staging-b oauth3 core

This deploys the same `oauth3-server/server` tree as the staging `oauth3` project under
`/oauth3b`. The daemon gives each isolated project its own persistent data volume and derives
`SEAL_KEY` from the project-scoped dstack path `/tee-daemon/projects/oauth3b/seal`; no key is
committed or copied through the manifest.

From a checkout with the staging credentials loaded:

```sh
source ~/.tee-daemon-staging.env
OAUTH3_SERVER_DIR=~/projects/oauth3-server ./examples/oauth3-staging-b/deploy.sh
```

Verify both routes and compare the returned `tree_hash` values before using `oauth3b` as a
migration destination.

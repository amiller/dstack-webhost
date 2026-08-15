# Flow evidence — issue #43 (per-project description line on the landing card)

**Tier 2 (user-visible landing change).** The landing is a public directory with no
user sign-in — the only identity is the owner token, and descriptions render
identically for anonymous and owner viewers — so the flow is the public card list
itself. Verified with a real browser (Playwright/Chromium) against the daemon
running from this branch, with the acceptance content asserted in the DOM before
the shots were taken.

## Acceptance (from the issue)
- `Project` gains a `description` field (`proxy/projects.py`, alongside `source`/`ref`),
  set from the repo-committed manifest and returned in `/_api/projects`.
- On the landing at `/`, each public app's card shows its manifest description as one
  line under the app name; a project with no description renders exactly as it does
  today — no empty element, no placeholder text.

## What changed
- `proxy/projects.py` — `description: str = ""` alongside `source`/`ref`. `asdict`
  serialization means `/_api/projects` (and `/_api/projects/<name>`) return it with
  no further change; old on-disk `project.json` loads keep working (absent key → `""`).
- `proxy/deploy.py` — `deploy()` reads it from the **repo-committed** manifest
  (`repo_manifest`, i.e. the `project.json` inside the cloned/extracted tree), so
  `tree_hash` commits to the blurb, mirroring the `cap_add`/`operator_debug`
  precedent. `_deploy_image()` takes it from the API manifest — image deploys have
  no source tree.
- `proxy/ingress.py` — the root JSON listing carries `description` per project
  (this payload is what the landing fetches).
- `proxy/templates/index.html` — `cardHTML()` renders `<p class="desc">…</p>` directly
  under the card head **only when** `description` is non-empty. No description → no
  element (acceptance).
- `test_daemon.py` — `test_landing_descriptions`: deploys two real projects (one
  whose repo `project.json` carries a description, one without), asserts the API
  surfaces (`/_api/projects`, root listing) and the rendered DOM, and captures the
  evidence shots below.

## How it was driven
`test_landing_descriptions` in `test_daemon.py` (suite green, `=== ALL TESTS PASSED ===`,
log at `~/paseo-batch/out/43/test.log`):

| project | repo manifest | asserted |
|---|---|---|
| `card-with-desc` | `project.json` with `"description": "A tiny static app used to prove the description line."` | deploy response + `/_api/projects` + root JSON carry the text; card renders `.desc` with exactly that text, exactly one element, directly under the card head (`02-described-card.png`) |
| `card-no-desc` | no `project.json` at all | all three surfaces report `description: ""`; card renders **zero** `.desc` elements — identical structure to before (`03-undescribed-card.png`) |

Screenshots (non-empty, `test -s`):
- `01-landing.png` — full landing page from the daemon running this branch, both test
  cards in the attested layer.
- `02-described-card.png` — the described card: name, description line under it, then
  the unchanged mode/source/tree rows and actions.
- `03-undescribed-card.png` — the undescribed card: renders exactly as today — no
  description element, no placeholder.

## What I could NOT verify (operator-run)
The issue's Flow says "deploy two projects to webhost-staging". Deploying *projects*
to the running staging CVM works from this box, but the running daemon there predates
this branch, so descriptions would not render until the daemon image is rebuilt and
the CVM redeployed — `ship-fix.sh staging` needs
`~/projects/hermes-agent/docker-compose.webhost-staging.yaml` + ghcr push creds +
`.env.webhost-staging`, which are not on this box (`specs/box-inventory.md`: "Not
here"). Same remaining operator step as #88 (PR #102): after the staging redeploy, a
staging-URL screenshot of a described + undescribed card closes the loop. Everything
short of that (code, API surfaces, real-browser render, regression test) is verified
here.

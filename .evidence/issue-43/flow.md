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

## Staging walk (2026-08-19, closes the Flow's webhost-staging half)

The daemon branch was deployed to webhost-staging through the capdel-brokered path
(`~/bin/ship staging-43` → build → ghcr push → `phala deploy` → compose digest+
`DAEMON_COMMIT` pinned), authenticated transcript:

```
    staging-43 -> 28c363e9
    pushed: ghcr.io/amiller/tee-socket-proxy@sha256:3edc3ccb0b0898af457e79bb112caca362ca3a441e583c81472306ad745c4292
==> 4/4 upgrade webhost-staging CVM
GET https://78ffc78c…-8080.dstack-pha-prod7.phala.network/_api/version
{"version": "dev", "commit": "28c363e9"}
OK: webhost-staging is running 28c363e9
```

Two projects deployed via `POST /_api/projects` (multipart tarball = repo-committed
tree; `mode: attested` so both render on the public landing):

- `card-with-desc` — tarball carries `project.json` with the same description as the
  local test; deploy response + `/_api/projects` returned
  `"description": "A tiny static app used to prove the description line."`
- `card-no-desc` — tarball with only `index.html`; all surfaces `"description": ""`

Walked in the envoy/neko real browser (not CDP — LESSONS) at
`https://78ffc78c25e0c8a9e64bb3a969ba6f226abae62d-8080.dstack-pha-prod7.phala.network/`,
asserting `location.href` before trusting the page (one silent navigate failure was
caught exactly this way and retried). DOM assertions, evaluated in-page after the
cards rendered:

```json
{"described_present":true,"undescribed_present":true,
 "described_text":"A tiny static app used to prove the description line.",
 "desc_directly_under_head":true,"described_desc_count":1,
 "undescribed_desc_count":0,"undescribed_head_next_is_meta":true,"total_cards":11}
```

— the described card shows exactly the manifest line, one element, directly under the
name; the undescribed card renders **zero** `.desc` elements with the card head
followed directly by the meta rows (identical to pre-change structure).

Screenshots (non-empty, `test -s`; viewport geometry verified at capture time — both
cards and the desc line inside the frame):
- `04-staging-landing.png` — the staging landing's attested layer with both test cards.
- `05-staging-cards.png` — `card-no-desc` (left) and `card-with-desc` (right) side by
  side: the description line appears under one name and not the other; everything
  else about the two cards is identical.

Both test projects were deleted after capture (`DELETE /_api/projects/<name>` → 200);
`/_api/projects` confirms they are gone and the daemon still reports `28c363e9`.

## What I could NOT verify
Nothing staging-side remains. Two operational notes for the operator:
- The redeploy above went through `ship-fix.sh`, which does not pass
  `--pre-launch-script` — #117's two-runtime gVisor prelaunch (runsc + runsc-hostnet,
  installed on webhost-staging earlier tonight) is not re-applied by this path. Re-apply
  it before the `DAEMON_CONTAINER_RUNTIME=runsc-hostnet` follow-up named in #117.
- The description feature is in the deployed daemon now; real apps' manifests can add
  `description` lines and they will render on `/` without further daemon changes.

# Issue #88 — landing card flags image deploys with no source

## Acceptance (from the issue)
A visitor can tell from the landing card alone, without clicking, that an
image-runtime app has no source to walk. For a card whose `source` is empty
(egress-vpn, twitter-debug), the card says so — wording
"image deploy — digest-pinned, no source" — and the link is relabelled away
from "verify" (→ "attestation"). Cards WITH a real source repo + commit keep
the "verify" wording unchanged. The verify page itself is not touched.

## What changed
`proxy/templates/index.html`, `cardHTML()` only:
- `source` row for empty `source` now reads **"image deploy — digest-pinned, no source"**
  (was the misleading `"local"`). Empty source only ever occurs for image deploys —
  git/static deploys require a source (`deploy.py` raises otherwise) — so this label
  is exact, not a guess.
- the attested card's link is relabelled **"attestation ↗"** when `source` is empty,
  **"verify ↗"** unchanged when a source repo + commit exist.

No Python change; no API/JSON change; `verification.html` untouched (issue: "the
verify page itself is correct and must not change").

## Evidence (Tier 2) — local daemon, real browser
`landing.png` is a full-page Playwright render of the landing from the same
`test_landing_cards` session. The test deployed two real projects to the daemon
running from this worktree and asserted the acceptance content BEFORE capturing
the shot (so the image is guaranteed to show it):

| project | deploy | asserted on the rendered card |
|---|---|---|
| `card-img-nosrc` | image, **no source**, attested | `"digest-pinned, no source"` present; link text `"attestation"` (1); `"verify"` absent (0) |
| `card-git-src` | static, git source + commit, attested | link text `"verify"` kept (1) |

Suite: `test_daemon.py` → **ALL TESTS PASSED** (incl. new `test_landing_cards`),
log at `~/paseo-batch/out/88/test.log`.

## What I could NOT verify (operator-run)
This is a **local daemon** render, not the remote staging CVM. Getting the new
template onto `…dstack-pha-prod7.phala.network` requires rebuilding + pushing the
daemon image and redeploying the CVM; the ghcr push creds / compose file are not on
this box (`specs/box-inventory.md`: "Not here: docker-compose.webhost-staging.yaml,
ghcr push credentials"). Per the scope-down rule the verifiable subset (code + real
browser render + regression test) is shipped here; the staging-CVM redeploy + a
staging-URL screenshot is the remaining operator step.

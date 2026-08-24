# Flow evidence — issue #60 (Paseo work dashboard)

**Tier 2 (user-visible page).** No auth/signed-in state applies — this is a static report
section in the Evidence Report, not an app behind login. Per the repo RUBRIC "Functional QA",
verification = real-browser render (Playwright/Chromium) + screenshot + content assertion +
no-error-text check. (The CONSTITUTION signed-in walk is for webhost-apps surfaces; there is
no identity to sign in with for a report page.)

## How it was driven
1. `python3 reports/gen-work-dashboard.py --standalone --out .preview.html` →
   rendered `01-work-dashboard.png` (full-page, Chromium @2x).
2. Wired into `~/paseo-batch/smoke/swarm-report.sh` (appends the fragment to
   `swarm-section.html`); ran `swarm-report.sh` + `generate-report.sh` to rebuild
   `~/paseo-batch/out/smoke/index.html`; screenshotted `section.work` inside the live
   Evidence Report → `02-folded-into-evidence-report.png` (+ context `02b-…`).
3. Asserted via `page.inner_text` / `eval_on_selector_all` (see /tmp/assert.py, /tmp/shot2.py).

## Acceptance content — asserted present in the render
- ✅ Recent **runs**: issue → branch → verify (pass/fail) → PR · merge state (12 rows,
  e.g. #92 `staging-92` verify=pass PR#98 open; #91 `staging-91` pass PR#97 merged).
- ✅ **PRs with merge + pass/fail state** (open PRs first, then recently merged).
- ✅ **ready/operator-ask queue**: #60 `operator-ask`.
- ✅ **Per-section last-run timestamp** on all three subsections
  (`last run 6d ago`, `last run 0m ago`, `last run 0m ago`).
- ✅ **>24h stale → RED**: "Recent runs — STALE" carries the `stale` class (RED background)
  — genuine, not fabricated: no tee-daemon lane run has been logged since #92 (2026-07-21).
- ✅ **Generated from `gh` + lane logs** (`gh pr/issue list` + `~/paseo-batch/out/<N>/result.json`).
- ✅ **Folded into the one Evidence Report (Phase 5)** — `section.work` present inside
  `out/smoke/index.html`; **not a new surface** (no new served page; `reports/` is
  Pages-excluded; the ad-hoc `reports/batch-latest.html` is removed).
- ✅ No `Traceback` / `Error` / `None` / `NaN` in the rendered DOM.

## What is NOT verified here
- The **public** Evidence Report URL (`amiller.github.io/dstack-webhost/report/`) — publishing
  pushes to `main`, which workers never do; the next `refresh-report.sh` cron run mirrors it.
- The `swarm-report.sh` wiring is a live, non-git box file (engine is versioned in this PR).

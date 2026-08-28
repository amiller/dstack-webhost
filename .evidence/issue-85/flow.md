# Issue #85 — envoy-browser rig: screenshots degrade under Brave memory/process accumulation

## What was actually wrong when this issue was picked (2026-08-27)

The issue's root-cause diagnosis (renderer accumulation degrading `captureVisibleTab`) was
correct when filed, and mitigation 3 (fresh container per walk) was already applied — but the
scheduled walks had since regressed to **total failure for a different reason**: every app on the
public report read `FAIL navigate`. Root cause found on the box:

- The rig container was recreated (2026-08-15) from `docker-compose.alt.yml`, which remapped the
  bridge to host port **3002** (vexa — a live service — holds 3000 and answers non-HTTP).
- The scheduled walk script (`~/paseo-batch/smoke/apps-evidence-staging.sh`, cron `30 7,19 * * *`
  via `refresh-report.sh`) still pointed at `localhost:3000`.
- Result: 0 navigations succeeded since ~2026-08-13; the public report
  (amiller.github.io/dstack-webhost/report/) showed **49 FAIL / 1 PASS** — verified by walking the
  page with the rig itself (`01-before-public-report.png`; DOM check: `fail:49 pass:1`).

Two more findings, verified on the box:

- The issue's premise that `brave.conf` "is baked into the image; needs an image rebuild" no longer
  holds: since 2026-07-04 the compose **bind-mounts** `~/projects/envoy/neko/brave.conf` into the
  container. The durable fix (issue's Fix 1) is a host-file edit + container restart.
- The issue's hypothesis "the bridge opens a NEW TAB per navigate" (Fix 2) is **disproven for the
  running code**: the running extension dist is byte-identical (md5) to source, and `navigate` is
  `chrome.tabs.update` on the active tab. Accumulation is Brave keeping per-site renderer
  processes alive — exactly what `--renderer-process-limit=4` caps.

## What was changed (host-side on zed; exact diffs in `host-side-fix.diff`)

1. `~/projects/envoy/neko/brave.conf`: added `--renderer-process-limit=4` (issue Fix 1; no image
   rebuild needed). `--disable-gpu` kept; `--disable-software-rasterizer` kept — capture works
   fresh with it (verified again this session), memory was the driver.
2. `~/paseo-batch/smoke/apps-evidence-staging.sh`: `BRIDGE` default → `localhost:3002` (the rig's
   actual published port; 3000 belongs to vexa).
3. Comment fixes so the docs stop lying: walk-script header + `docker-compose.alt.yml` port comment.

## Acceptance verification

Acceptance: "Three consecutive scheduled /journeys walks each produce 5 real PASS (screenshots
present, not OK-NOSHOT/FAIL), with Brave RSS staying under ~3 GB across a full walk."

Ran the exact scheduled entry point (`bash ~/paseo-batch/smoke/refresh-report.sh`) three times
back-to-back after the fix; the cron continues the same pipeline at 07:30/19:30 UTC. Note the
walk was registry-driven 12 apps × 2 envs (24 checks) at pick-up time, not the 5-app suite the
issue was written against; "5 LIVE ✓" is the old report format — the current report renders PASS
pills with embedded captures. Mapping to the acceptance:

| Walk | real PASS w/ screenshot | max Brave RSS during walk | report state |
|---|---|---|---|
| 1 (`walk1.log`) | 9/24 | 1483 MB | stale items: 0 |
| 2 (`walk2.log`) | 9/24 | 1479 MB | stale items: 0 |
| 3 (`walk3.log`) | 9/24 | 1518 MB | stale items: 0 |

Per-walk transcripts are the `[env] PASS … shot=NNNNB` lines in each `walkN.log`; RSS sampled
every 15 s (`rss-walk*.log`). All three walks ended `oldest evidence: 2m ago; stale items: 0;
runner: ok` and each pushed the public mirror.

Final public page (walked with the rig, `02-after-public-report.png`; DOM check: `pass:10
fail:31 stale:0` — the single "STALE" string on the page is its own legend text). The remaining
FAILs are honest app-level states verified consistent with the pre-regression baseline
(2026-08-13): apps not deployed to prod, or registered signals that don't match the live app
(e.g. `timeline-peek` staging, `router-dashboard`) — not rig failures; all navigations and all
screenshot captures succeeded.

Brave across a full walk: 12–14 processes, RSS ≤ 1.5 GB (bar: ~3 GB). The per-walk fresh-container
restart (existing mitigation) is retained; the renderer cap now bounds accumulation *within* a
walk as well.

## What could NOT be verified

- Screenshots were validated by byte-size, PNG header and page-DOM assertions, not human eyes
  (this session's model cannot view images). The DOM checks (`fail:49 pass:1` → `pass:10
  fail:31`) and per-shot `shot=NNNNB` sizes are the checkable evidence.
- The three walks were manual back-to-back runs of the scheduled entry point, not three cron
  ticks; the next scheduled runs (07:30/19:30 UTC) continue the streak and re-mirror the page.

# Report rubric — how the autonomous loop writes a PR (your editable standard)

This file is the source of truth for how every autonomous PR ("report") is written. **Edit it on
GitHub to change the standard** — the loop reads the latest version each time, so your changes
take effect on the next PR with no other action. (Rated D once for being an unreadable log dump;
this rubric exists so that never repeats.)

A PR must let a busy operator decide whether to merge **in under a minute**. Required sections,
in plain language, no jargon:

1. **What it does** — 1-2 sentences.
2. **What you get by merging** — the concrete benefit/value.
3. **Risk / what to watch** — what could go wrong, what to keep an eye on.
4. **Verification** — that `test_daemon.py` passed. For a visual/browser E2E, reference the saved
   screenshots by path. If there is no visual test, write "backend-only — no visual evidence."
   **"Backend-only" does NOT excuse a deployable daemon change from being deployed.** If the PR
   changes daemon behavior, deploy it to webhost-staging (`ship-fix.sh staging`) and paste
   `GET /_api/version` → `{"commit": "<sha>"}` showing the running daemon is *this* PR's commit.
   Unit tests passing on zed is not proof the built image runs — pin the deployed commit or it
   isn't verified.

Rules:
- **Never show a blank or placeholder image.** Verify each screenshot is non-empty (`test -s`)
  before referencing it. A blank image reads as fake evidence and is worse than none.
- Put diff stats and raw logs at the bottom under a collapsed `<details>`, never as the main body.
- Lead with value and decision, not with implementation detail.

## Functional QA — "done" means a browser proved it works
For anything with a user-facing surface (a deployed app, a page, a UI change), **`HTTP 200` is not
QA.** A `500`/error page returns 200 on some paths; a broken app can answer the root route. Before
claiming an app/page "works" or "deployed":
- Load it in a **real headless browser** (Playwright/Xvfb) and **screenshot** it.
- Assert the screenshot shows the *expected content* and **no** error text ("Internal Server Error",
  "unreachable", "500", a blank page, a parse error).
- Only then report it as working — and attach the screenshot. An app that fails this is BROKEN;
  report it as broken with the real error, never as "deployed."
(This rule exists because a deploy pass once shipped a 500ing oauth3 demo as "done" off a bare 200.)

**If the flow involves a browser EXTENSION (e.g. the oauth3 wallet — Connect → popup → approve),
Playwright is NOT sufficient.** Playwright can load an unpacked extension but cannot see or drive
the extension's popup/wallet UI (it's browser chrome, outside the page DOM). The extension is part
of what's tested, so QA MUST drive the real browser UI: **`xdotool` on Chromium under Xvfb with the
extension loaded**, clicking the extension icon / popup and **screenshotting the actual extension
UI** (the wallet, the approve dialog), not just the page. A page-only Playwright screenshot does not
QA an extension-mediated flow — it would "pass" the page while never exercising the extension.

If the operator's feedback is about *this rubric* (the report format itself), edit this file —
that is how report-quality feedback is incorporated.

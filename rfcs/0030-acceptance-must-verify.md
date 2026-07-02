# RFC 0030: Acceptance Must Verify, Not Ping (and who verifies the verifier)

**Status**: Draft

## Summary
The autonomous loop's acceptance gates are shallow — health pings and file-existence checks —
so they report PASS on things that are not verified. This is the over-claim failure the loop exists
to prevent, occurring at the one layer meant to catch it. Fix acceptance to **drive the real user
journey and assert on what it observes** (screenshot content, not screenshot existence), make the
report honest about what was actually tested, and — the load-bearing part — recognize that **the loop
cannot certify a fix to its own verifier.** That certification is a one-time out-of-loop act.

## Problem
Observed live: `browser-pool` shows a green **PASS** while its root serves `{"error":"Not found"}`
and its own line reads *"e2e: no e2e-verdict.md yet (extension click-through pending)."* Its
acceptance (`acceptance-browser-pool.sh`) passes on:
```
GET /browser-pool/health → 200 & contains "max_instances"
POST /browser-pool/lease → contains "bridge_url"
ls screenshots/*.png     → a file exists
```
Two failure modes, one false alarm:
- **False alarm:** browser-pool is a backend service with no root page, so `/` = 404 is fine — the
  check hits `/health`. Displaying the 404 root beside a green PASS is misleading, but the PASS is
  not asserting the 404 is good.
- **Real:** `ls screenshots/*.png` proves a PNG *exists*, not that a journey happened or the image
  shows anything. That is file-existence theater, the opposite of "screenshots from a user journey
  start to finish." And PASS is painted green while the app's own e2e line says the journey is pending.

The gold standard already exists — the extension `e2e-flow.mjs` (navigate → connect → approve →
poll a real `tok-…`, screenshotting each step). Almost nothing else uses it; most apps get pinged.

## The deeper problem: who verifies the verifier
You cannot ask the loop to fix its own acceptance gate and let its acceptance certify the fix. If the
loop could tell the new 404-detector works, it could already have told the 404 was a FAIL — the
defect and the check for the defect are the same faculty. **A verifier's trustworthiness is not
self-establishable.** It must be shown once, from outside: point the gate at a known-**good** app and
a known-**broken** one and confirm PASS and FAIL respectively — a smoke-detector-detects-smoke test.
After that, the loop may *propagate* the pattern to more apps (each propagation is checkable against
the certified harness); it may not *re-certify* the harness.

## Design
1. **Drive the journey.** For anything user-facing, acceptance navigates and acts (the `e2e-flow.mjs`
   pattern), it does not ping an endpoint. Backend-only services (browser-pool) declare themselves
   `surface: backend` and their acceptance targets their real endpoints — and they do **not** receive
   a green "works for users" PASS; they get a distinct "backend reachable" status.
2. **Assert on screenshot content, not existence.** Each captured frame is checked: non-blank
   (byte-size / pixel-variance floor), expected text present, no error strings ("Not found",
   "Internal Server Error", "Failed to load", a blank body). A PNG that fails these is evidence of
   failure, not of success.
3. **PASS is journey-verified, not backend-up.** Three outcomes, not two: `verified` (journey driven
   to its real end state — a token, a rendered result), `backend-ok` (endpoints reachable, journey
   not driven), `fail`. Only `verified` is green. `backend-ok` while e2e is pending is amber, never
   green.
4. **Report honesty.** Never show a status beside a non-2xx of the *tested* surface without labeling
   it; show the actual tested URL and the actual assertion that passed; never render green while any
   sub-check is "pending."
5. **The certification harness (out-of-loop).** A `certify-acceptance` step, run by a human/laptop,
   drives the gate against a known-good and a known-broken fixture and asserts PASS/FAIL. The loop's
   schedule does not include it. This is the RFC 0023 "capability held only by the laptop" applied to
   trust in the verifier itself.

## Relation to other RFCs
- **RUBRIC.md** (the report standard) — this is its enforcement: "HTTP 200 is not QA" was prose; this
  makes it a gate that fails closed. The RUBRIC's browser-QA rule becomes the screenshot-content assert.
- **RFC 0023** (autonomous loop / laptop-gated capability) — verifier-certification is another
  capability the loop cannot self-hold; it belongs with the human, same as prod deploy.
- **RFC 0020** (facts not verdicts) — a report showing `backend-ok` vs `verified` is facts; painting
  green over "pending" is a manufactured verdict.

## Out of Scope
- The specific browser-driving harness (envoy/e2e-flow.mjs mechanics) — reused, not respecified.
- Per-app journey definitions — each app owns its journey; this RFC sets the floor every journey meets.
- Making the loop author *new* journeys — it may, but the certification that a journey's gate actually
  fails on a broken app stays out-of-loop.

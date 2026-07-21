# verify/fixtures/

Real-world attestation bundles captured from a running tee-daemon, used as test
fixtures so the legs of `verify()` can be exercised against a real RFC 0020 bundle
instead of hand-built dicts.

## `bundle-prod-oauth3.json`

- **Source URL:** `GET https://pod.dstack.soc1024.com/_api/verification/oauth3`
- **Captured:** 2026-07-20
- **Captured by:** `curl -sS -H "Accept: application/json" <URL>`
- **Verbatim:** the bytes on disk are exactly what the endpoint returned — not
  re-serialized, not re-indented, not augmented. The endpoint is deliberately
  unauthenticated (RFC 0015: a relying party should not need the admin token to
  verify what is running), so committing it leaks nothing.

RFC 0020 top-level shape: `schema_version, platform_quote, webhost_app_id,
onchain, gateway, app, audit`. The source tree hash lives at
`app.source.tree_hash`.

## `bundle-prod-oauth3-tampered.json`

Byte-identical to `bundle-prod-oauth3.json` except that **one hex character** of
`app.source.tree_hash` is flipped (`f` → `e` at the last digit). It is produced
by a first-occurrence-only byte replace, so the *copy* of the same tree hash that
appears inside the `audit` log (JSON-stringified in `audit[*].detail`) is left
intact — the two fixtures differ structurally at exactly one path.

Purpose: tests can assert that a verification check that should catch a tampered
`tree_hash` actually does, without an unrelated diff muddying the signal.

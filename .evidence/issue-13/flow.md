# Issue 13 flow evidence

Tier 2: the acceptance is a public relying-party flow, so there is no sign-in surface to exercise.
The walk used the Envoy real browser with navigation URLs asserted after each navigate.

1. Open `/isolation-probe/`. The page rendered **Isolation probe**, **Substrate claim**, and
   **Tenant evidence**; the latter contains the tenant's own `/proc` namespace values and the
   observed kernel/runtime signals. See `01-probe.png`.
2. Open `/_api/verification/isolation-probe?format=html`. The verifier rendered project
   `isolation-probe` in attested mode with source
   `https://github.com/amiller/dstack-webhost.git`, commit
   `4566512c87f8fca752d8e18b55a30e25c4effa47`, and tree hash
   `c46477a8f2223c2d086f2c93d09dbb019b10dfdd`. See `02-verifier.png`.

HTTP acceptance checks against deployed staging also confirmed the unauthenticated root listing
contains `isolation-probe` with `mode: "attested"`, non-empty `commit_sha` and `tree_hash`, and
`GET /_api/verification/isolation-probe` returns 200 with the same source pins.

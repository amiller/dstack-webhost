# RFC 0017: State Durability and Recovery Across CVM Lifecycle

## Summary
Make the deployed fleet and its private per-app state survive a CVM being recreated: externalize the daemon's volumes, provide an exportable manifest set, and a *pinned* restore that redeploys each app at its recorded commit/digest — refusing (not silently re-cloning) if the pin no longer matches.

## Problem
Recovery today is shallow. `recover_all()` in `proxy/runtimes.py` restarts runtimes and per-project containers from whatever is on disk under `/var/lib/tee-daemon`. That disk persists **only if** `DAEMON_VOLUME_NAME` / the data volume point at external named volumes; the default `daemon_data` volume in `docker-compose.yaml` is local to the CVM. Concretely, on a fresh CVM:

- **Project metadata** (`projects/<name>/project.json`) is gone unless the volume was external.
- **Project code** is re-cloned from `source`/`ref` or must be re-uploaded — and a tarball-uploaded project with no `source` cannot be reconstituted at all.
- **Private `dataDir`** (`/daemon-data/<name>`, per the per-project data volume) has no backup; it lives or dies with the volume.
- There is **no portable artifact** describing "this exact set of apps at these exact versions" that you could carry to a new CVM and replay.

So a CVM swap risks losing apps and private state, and there is no deterministic way to bring a fleet back at known-good versions. This is the "doesn't feel durable / won't survive the CVM changing" concern, and it is accurate.

## Files to Modify
- `proxy/projects.py` — export/import of the full manifest set.
- `proxy/deploy.py` — a pinned restore path that verifies `tree_hash` (git) or `image_digest` (image) against the recorded pin before serving.
- `proxy/runtimes.py` — `recover_all()` bootstraps from an import bundle when the registry is empty on boot.
- `proxy/ingress.py` — `GET /_api/export` (authed) and `POST /_api/import` (authed).
- `docker-compose.yaml` — document/require external named volumes for `daemon_data`, audit, tunnels, and the data volume.

## Implementation
1. **`GET /_api/export`** (authed): a JSON bundle of every project's manifest with its pins — `source`, `ref`, `commit_sha`, `tree_hash`, `image`, `image_digest`, `runtime`, `entry`, `port`, `mode`, `volumes` — plus audit-log references. Raw `env` secrets are **excluded** by default; secret continuity is the credential broker's job (RFC 0018), not a plaintext dump.
2. **`POST /_api/import`** (or a CLI): for each manifest, redeploy *pinned*. Git projects clone at `commit_sha` and `deploy.py` recomputes `tree_hash`; if it does not match the recorded pin, **error and skip** — no fallback to re-clone-latest, no masking. Image projects pull by `image_digest`. This reconstitutes the fleet on a fresh CVM deterministically.
3. **Operational requirement, documented:** `daemon_data`, the audit dir, the tunnel dir, and the per-project data volume must be external named volumes that outlive any single CVM (ties to the `DAEMON_VOLUME_NAME` / data-volume wiring already in `runtimes.py`). Update the `docker-compose.yaml` comments so this is not a silent footgun.
4. **Boot bootstrap:** in `recover_all()`, if the project registry is empty but an import bundle is present, restore from it before starting runtimes — so a fresh CVM with the export attached comes up as the previous fleet.
5. **Sealed `dataDir` snapshot (stretch, separate follow-up):** an export of `/daemon-data/<name>` encrypted under the dstack-derived key, restorable only inside the same app identity. The manifest export + external volumes is the MVP; the sealed-snapshot crypto is its own RFC and should not block this one.

## Testing & Validation Requirements
- Export the fleet, tear down every project, import → the identical set comes back running at the same pins.
- Tamper a `commit_sha`/`tree_hash` in the bundle → import errors on that project and does **not** silently re-clone latest.
- With the data volume external, `dataDir` contents written by an app survive a daemon/container restart.
- A fresh registry with an import bundle present bootstraps the fleet on `recover_all()`.

## Report Requirements
- A sample `GET /_api/export` bundle (secrets absent).
- Transcript of tear-down → import reconstituting the fleet at pinned versions.
- Demonstration that a wrong pin produces a hard error, not a silent reclone.

## Out of Scope
- Cross-CVM live migration and multi-CVM federation (RFC 0001 non-goal).
- Scheduled/automatic backups (manual export is the MVP).
- The sealed-`dataDir` encryption scheme (its own follow-up RFC).
- Secret continuity across the move — that is RFC 0018.

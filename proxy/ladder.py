"""RFC 0016 trust-ladder hint: one generated "next rung" line per project.

Pure, offline classifier — no network, no LLM, no filesystem. It reads only the
manifest fields that ProjectStore already persists, so the console can label each
app's current standing and the single cheapest action to climb.
"""

# Ladder rungs (stable — do not renumber):
#   0  unattestable    — no re-cloneable source (missing / NONE / tarball://)
#   1  dev (private)    — re-cloneable source, not listed
#   2  dev (public)     — re-cloneable source, listed
#   3  attested         — promoted, tree hash pinned
#   4  evidence-carrying (RFC 0021, next target above attested)
#   5  curated          (RFC 0022, not yet built)


def _recloneable(source: str) -> bool:
    s = (source or "").strip()
    return bool(s) and s.upper() != "NONE" and not s.startswith("tarball://")


def ladder_hint(project: dict) -> dict:
    """Classify a project's trust-ladder standing from its manifest.

    `project` is the manifest dict (dataclasses.asdict of a Project). Returns
    {"rung": int, "label": str, "next": str}. Rung numbering is documented above
    and MUST stay stable. Rules are applied in order:

      1. source missing / NONE / tarball://  -> rung 0
      2. dev + re-cloneable + not public      -> rung 1
      3. dev + public + source                -> rung 2
      4. attested                             -> rung 3

    Evidence-dir presence (rung 4) is not in the manifest, so an attested project
    always gets the generic "next: evidence" hint rather than a filesystem probe.
    """
    name = project.get("name", "")
    mode = project.get("mode", "dev")

    if not _recloneable(project.get("source", "")):
        return {
            "rung": 0,
            "label": "unattestable",
            "next": "no re-cloneable source — adopt into a git repo "
                    "(source+ref+commit) to become attestable",
        }

    if mode == "attested":
        return {
            "rung": 3,
            "label": "attested",
            "next": "attested — next: capability statement + evidence artifacts "
                    "(RFC 0021); curated listing (RFC 0022) not yet built",
        }

    if not project.get("public", False):
        return {
            "rung": 1,
            "label": "dev (private)",
            "next": "set public:true to be listed, or promote to attested",
        }

    return {
        "rung": 2,
        "label": "dev (public)",
        "next": f"promote to attested (POST /_api/projects/{name}/promote) — "
                "pins the tree hash and opens the public verifier endpoints",
    }

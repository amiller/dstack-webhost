"""
Tests for the captured RFC 0020 bundle fixtures (issue #89).

These guard the fixtures themselves: that the good bundle parses through
``_parse_bundle`` and that the tampered twin differs from it at exactly one
path (``app.source.tree_hash``) by exactly one hex character — so a drifted or
hand-edited fixture cannot quietly widen the diff.
"""

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = FIXTURES / "bundle-prod-oauth3.json"
TAMPERED = FIXTURES / "bundle-prod-oauth3-tampered.json"


def _diff_leaves(a, b, path=()):
    """Yield (path, value_a, value_b) for every leaf that differs or is
    present on only one side. A structural/type/length mismatch is reported
    as a single diff at that node."""
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            if k not in a:
                diffs.append((path + (k,), "<missing>", b[k]))
            elif k not in b:
                diffs.append((path + (k,), a[k], "<missing>"))
            else:
                diffs.extend(_diff_leaves(a[k], b[k], path + (k,)))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append((path, a, b))
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diffs.extend(_diff_leaves(x, y, path + (i,)))
    elif a != b:
        diffs.append((path, a, b))
    return diffs


def test_good_fixture_parses_via_parse_bundle():
    """The captured good bundle must parse through _parse_bundle without
    raising (acceptance: every leg of verify() needs a real bundle to load)."""
    from verify.facts import _parse_bundle

    bundle = json.loads(GOOD.read_bytes())
    parsed = _parse_bundle(bundle)  # must not raise; returns the shared EvidenceBundle
    # Prove we actually loaded the real bundle, not an empty fallback.
    assert isinstance(parsed.audit, list) and len(parsed.audit) > 0
    assert parsed.app.source.tree_hash
    assert parsed.platform_quote.get("quote")
    assert parsed.onchain.chain_id == 0
    print("test_good_fixture_parses_via_parse_bundle: PASS")


def test_tampered_fixture_differs_only_at_tree_hash():
    """The two fixtures differ structurally at exactly app.source.tree_hash,
    and by exactly one hex character."""
    good = json.loads(GOOD.read_bytes())
    tampered = json.loads(TAMPERED.read_bytes())

    diffs = _diff_leaves(good, tampered)
    paths = [".".join(map(str, p)) for p, _, _ in diffs]
    assert paths == ["app.source.tree_hash"], (
        f"expected diff only at app.source.tree_hash, got: {paths}"
    )

    g_th = good["app"]["source"]["tree_hash"]
    t_th = tampered["app"]["source"]["tree_hash"]
    assert len(g_th) == 64 and len(t_th) == 64
    assert all(c in "0123456789abcdef" for c in g_th + t_th)
    hamming = sum(a != b for a, b in zip(g_th, t_th))
    assert hamming == 1, f"expected tree_hash Hamming distance 1, got {hamming}"
    print("test_tampered_fixture_differs_only_at_tree_hash: PASS")


if __name__ == "__main__":
    test_good_fixture_parses_via_parse_bundle()
    test_tampered_fixture_differs_only_at_tree_hash()
    print("\n=== ALL FIXTURE TESTS PASSED ===")

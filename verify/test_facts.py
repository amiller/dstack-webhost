"""Tests for verify() Facts library."""

import asyncio
import json

from verify import (
    Facts,
    verify_from_bundle,
    BundleParseError,
)


def test_parse_valid_bundle():
    """Test parsing a valid bundle."""
    bundle = {
        "project": {
            "name": "test-app",
            "source": "https://github.com/test/repo",
            "commit_sha": "abcd1234",
            "tree_hash": "deadbeef",
            "mode": "attested",
        },
        "quote": {
            "pubkey": "02abcdef",
            "signature_chain": [{"authority": "test-app-id"}],
        },
        "audit": [
            {"action": "promote", "timestamp": 1234567890}
        ],
    }

    facts = asyncio.run(verify_from_bundle(bundle))

    assert facts.attestation_kind == "daemon-vouched"
    assert facts.source.tree_hash == "deadbeef"
    assert facts.app_id == "test-app-id"
    assert facts.binding.app_pubkey == "02abcdef"
    print("test_parse_valid_bundle: PASS")


def test_parse_invalid_bundle():
    """Test parsing an invalid bundle raises error."""
    try:
        from verify.facts import _parse_bundle
        _parse_bundle("not a dict")
        assert False, "Should have raised BundleParseError"
    except BundleParseError:
        print("test_parse_invalid_bundle: PASS")


def test_facts_to_dict():
    """Test Facts serialization."""
    bundle = {
        "project": {
            "name": "test-app",
            "source": "https://github.com/test/repo",
            "commit_sha": "abcd1234",
            "tree_hash": "deadbeef",
            "mode": "attested",
        },
        "quote": {
            "pubkey": "02abcdef",
            "signature_chain": [{"authority": "test-app-id"}],
        },
        "audit": [],
    }

    facts = asyncio.run(verify_from_bundle(bundle))
    d = facts.to_dict()

    assert d["schema_version"] == "1.0"
    assert d["attestation_kind"] == "daemon-vouched"
    assert d["source"]["tree_hash"] == "deadbeef"
    assert isinstance(d["errors"], list)
    print("test_facts_to_dict: PASS")


def test_empty_bundle():
    """Test handling of minimal bundle."""
    bundle = {
        "project": {
            "name": "test-app",
            "mode": "attested",
        },
        "quote": {},
        "audit": [],
    }

    facts = asyncio.run(verify_from_bundle(bundle))
    # Should have errors about missing fields
    assert len(facts.errors) > 0
    print(f"test_empty_bundle: PASS ({len(facts.errors)} errors)")


def test_report_data_preimage():
    """Test report_data preimage computation."""
    from verify.facts import _compute_report_data_preimage

    preimage = _compute_report_data_preimage(
        app_id="test-app-id",
        name="test-app",
        tree_hash="abcd1234",
        app_pubkey="02abcdef",
    )

    assert len(preimage) == 128  # SHA-512 hex = 128 chars
    assert all(c in "0123456789abcdef" for c in preimage)
    print(f"test_report_data_preimage: PASS ({preimage[:16]}...)")


if __name__ == "__main__":
    test_parse_valid_bundle()
    test_parse_invalid_bundle()
    test_facts_to_dict()
    test_empty_bundle()
    test_report_data_preimage()
    print("\n=== ALL TESTS PASSED ===")

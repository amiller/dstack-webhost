"""
Tests for the verify() Facts library — the RFC 0020 validation matrix.

These cover the five cases in RFC 0020 §"Testing & Validation Requirements"
against the captured fixtures (verify/fixtures/), plus the no-verdict property
that is the RFC's central claim: the library returns Facts and never throws a
verdict; accept/reject lives in the consumer (see verify/policies.py).
"""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest

from verify import Facts, verify_from_bundle, BundleParseError
from verify.facts import _compute_report_data_preimage, _parse_bundle
from verify.policies import allowlist_policy, strict_policy
from verify import (
    SCHEMA_VERSION,
    AppInfo,
    EvidenceBundle,
    GatewayInfo,
    OnchainInfo,
    OperatorDebugInfo,
    SourceInfo,
)

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = FIXTURES / "bundle-prod-oauth3.json"
TAMPERED = FIXTURES / "bundle-prod-oauth3-tampered.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_bytes())


def _verify(path: Path) -> Facts:
    """verify_from_bundle is async (it shares the live verify() core); run it."""
    return asyncio.run(verify_from_bundle(_load(path)))


# --- RFC 0020 case: tampered tree_hash --------------------------------------

def test_tampered_tree_hash_surfaces_mismatch_and_verify_returns():
    """Tampering app.source.tree_hash surfaces in Facts.errors[] (the RFC 0025
    binding leg catches the tree_hash not being committed to by the quote), and
    verify() RETURNS rather than raising — the library renders no verdict
    (RFC: "it does not throw a verdict")."""
    bundle = _load(TAMPERED)
    facts = _verify(TAMPERED)  # must NOT raise

    assert isinstance(facts, Facts)
    # the library surfaces the claimed (tampered) value as a fact, verbatim
    assert facts.source.tree_hash == bundle["app"]["source"]["tree_hash"]
    assert facts.source.tree_hash != _load(GOOD)["app"]["source"]["tree_hash"]
    # the binding leg records a tree_hash mismatch (the staging quote's
    # report_data is empty/zero, so the claimed tree_hash is not bound)
    assert any("binding" in e or "tree_hash" in e for e in facts.errors), (
        f"expected a binding/tree_hash mismatch in errors, got: {facts.errors}"
    )
    print(f"test_tampered_tree_hash_surfaces_mismatch_and_verify_returns: PASS "
          f"({len(facts.errors)} errors, returned without raising)")


# --- RFC 0020 case: non-anchored ecosystem ----------------------------------

def test_non_anchored_ecosystem_surfaces_chain_id_zero_no_crash():
    """On a non-anchored ecosystem (chain_id 0, e.g. pha-prod), the library
    surfaces onchain_approved=False / chain_id=0 as FACTS and does NOT crash or
    treat it as an error — non-anchored is an expected state, not a failure."""
    facts = _verify(GOOD)  # must NOT raise

    assert facts.onchain.chain_id == "0"
    assert facts.onchain.approved is False
    # chain_id 0 is an expected state — must not be reported as an approval error
    assert not any("onchain_approved" in e or "not approved" in e.lower() for e in facts.errors), (
        f"chain_id 0 leaked into errors as an approval failure: {facts.errors}"
    )
    print("test_non_anchored_ecosystem_surfaces_chain_id_zero_no_crash: PASS "
          "(chain_id=0, approved=False, no crash)")


# --- RFC 0020 case: two consumer policies, one Facts object -----------------

def test_two_policies_disagree_on_same_facts():
    """Two consumer policies over the SAME Facts object disagree — one accepts,
    one rejects. Demonstrates 'no universal verdict' (RFC 0020's central claim)
    rather than asserting it in prose."""
    facts = _verify(GOOD)

    # Policy (a): allowlist the bundle's own tree_hash -> ACCEPTS
    accepts = allowlist_policy(facts, {facts.source.tree_hash})
    # Policy (b): strict — needs quote_valid && onchain_approved && gateway &&
    # chain_id==8453. On the staging fixture the onchain/gateway/chain_id legs
    # are False/empty/0, so strict rejects regardless of the quote leg.
    rejects = strict_policy(facts, expected_chain_id=8453)

    assert accepts is True, "allowlist policy should accept its own tree_hash"
    assert rejects is False, (
        f"strict policy should reject: quote_valid={facts.quote.quote_valid}, "
        f"approved={facts.onchain.approved}, "
        f"zt_cert_ref={bool(facts.channel.zt_cert_ref)!r}, "
        f"chain_id={facts.onchain.chain_id!r}"
    )
    assert accepts != rejects, "the two policies must disagree on the same facts"
    print(f"test_two_policies_disagree_on_same_facts: PASS "
          f"(allow={accepts}, strict={rejects} — same Facts)")


def test_allowlist_policy_catches_tampered_tree_hash():
    """Policy (a) is the policy that catches a tampered tree_hash: the consumer
    allowlists the GOOD tree_hash, and the tampered bundle is rejected. This is
    the consumer-side check that makes the bundle's claimed tree_hash
    meaningful — the library surfaces it, the policy enforces it."""
    good_facts = _verify(GOOD)
    tampered_facts = _verify(TAMPERED)
    allowlist = {good_facts.source.tree_hash}  # consumer trusts the good tree_hash

    assert allowlist_policy(good_facts, allowlist) is True
    assert allowlist_policy(tampered_facts, allowlist) is False
    print("test_allowlist_policy_catches_tampered_tree_hash: PASS "
          "(allowlist accepts good, rejects tampered)")


def test_policies_are_ordinary_functions_not_on_facts():
    """The reference policies must NOT live on Facts or be produced by verify()
    — they are ordinary functions the consumer calls. Guards against the
    library growing a verdict return path (the regression RFC 0020 forbids)."""
    facts = _verify(GOOD)
    for name in ("policy", "verdict", "decide", "accept", "reject", "should_accept"):
        assert not callable(getattr(facts, name, None)), (
            f"Facts must not grow a {name}() method — policy lives in the consumer"
        )
    assert callable(allowlist_policy) and callable(strict_policy)
    print("test_policies_are_ordinary_functions_not_on_facts: PASS")


# --- RFC 0020 case: Facts exposes no verdict field --------------------------

def test_facts_exposes_no_verdict_field():
    """The library must not grow a verdict field later without failing this
    test. RFC 0020's central claim: the library renders NO verdict. (is_valid
    is a documented convenience for "errors list is empty", explicitly not a
    verdict.)"""
    forbidden = {"verdict", "accepted", "valid_overall", "approved_overall", "is_accepted"}
    facts = Facts()
    actual = set(facts.__dataclass_fields__)  # type: ignore[attr-defined]
    leaked = forbidden & actual
    assert not leaked, f"Facts must not expose a verdict field; found {leaked}"
    assert callable(facts.is_valid), "is_valid convenience must remain"
    print("test_facts_exposes_no_verdict_field: PASS")


# --- RFC 0025 preimage: the mechanism that catches tampering ----------------

def test_report_data_preimage_is_sensitive_to_tree_hash():
    """The RFC 0025 report-data preimage changes when tree_hash changes — so a
    real (non-empty) binding WOULD detect the tampering. This is the mechanism
    the binding leg relies on; the staging fixture's report_data is empty, so
    the leg records the mismatch as a fact instead of matching."""
    good = _load(GOOD)
    tampered = _load(TAMPERED)
    common = dict(
        app_id=good.get("webhost_app_id", ""),
        name=good["app"]["project"],
        app_pubkey=good["app"]["binding_quote"]["pubkey"],
    )
    pre_good = _compute_report_data_preimage(
        tree_hash=good["app"]["source"]["tree_hash"], **common)
    pre_tampered = _compute_report_data_preimage(
        tree_hash=tampered["app"]["source"]["tree_hash"], **common)
    assert len(pre_good) == 128  # SHA-512 hex
    assert all(c in "0123456789abcdef" for c in pre_good)
    assert pre_good != pre_tampered  # 1-char tree_hash flip changes the preimage
    print("test_report_data_preimage_is_sensitive_to_tree_hash: PASS")


# --- parse + serialize guards -----------------------------------------------

def test_parse_invalid_bundle_raises_and_verify_surfaces_it():
    """A structurally invalid bundle raises BundleParseError from _parse_bundle
    (the low-level helper), and verify_from_bundle() — the public entry point a
    consumer calls — surfaces it in errors[] rather than raising. The library
    never throws a verdict."""
    with pytest.raises(BundleParseError):
        _parse_bundle("not a dict")  # type: ignore[arg-type]

    facts = asyncio.run(verify_from_bundle({"app": "not-an-object"}))  # type: ignore[arg-type]
    assert isinstance(facts, Facts)
    assert any("parse" in e for e in facts.errors)
    print("test_parse_invalid_bundle_raises_and_verify_surfaces_it: PASS")


def test_facts_to_dict_is_json_serializable_and_round_trips():
    """Facts.to_dict() stays JSON-serializable and round-trips the facts a
    consumer or renderer (verify.md) reads — including the RFC 0029
    operator_debug fact, which must survive the verify() core."""
    bundle = _load(GOOD)
    facts = _verify(GOOD)
    d = facts.to_dict()

    assert d["schema_version"] == bundle["schema_version"]
    assert d["source"]["tree_hash"] == bundle["app"]["source"]["tree_hash"]
    assert d["onchain"]["chain_id"] == "0"
    assert d["onchain"]["approved"] is False
    assert "operator_debug" in d  # RFC 0029 fact preserved
    assert isinstance(d["errors"], list) and d["errors"]
    json.dumps(d)  # must not raise
    print("test_facts_to_dict_is_json_serializable_and_round_trips: PASS")


# --- producer <-> consumer round-trip (issue #92) ---------------------------

def _full_producer_bundle() -> EvidenceBundle:
    """Build a bundle via the producer path with every field set to a distinctive value.

    Mirrors what ``proxy/ingress.py`` does: populate the shared ``EvidenceBundle``
    dataclasses, then serialize. Any field the producer can set is set here."""
    app_pubkey = "02abcdef"  # valid hex (the binding preimage does bytes.fromhex on it)
    project = "rfc-roundtrip"
    webhost_app_id = "<webhost-app-id>"
    tree_hash = "deadbeef" * 8
    report_data = _compute_report_data_preimage(
        app_id=webhost_app_id, name=project, tree_hash=tree_hash, app_pubkey=app_pubkey,
    )

    bundle = EvidenceBundle()
    bundle.schema_version = SCHEMA_VERSION
    bundle.platform_quote = {"quote": "<raw-platform-quote>", "report_data": report_data}
    bundle.webhost_app_id = webhost_app_id
    bundle.onchain = OnchainInfo(
        chain_id=0,  # non-anchored (pha-prod), matches the served fixture
        kms_contract="0xKMS",
        dstackapp="0xDAPP",
        allowed_compose_hash="0xCOMPOSE",
        allowed_os_image="0xOSIMAGE",
    )
    bundle.gateway = GatewayInfo(
        domain="gateway.example",
        app_id="<gateway-app-id>",
        zt_cert_ref="<zt-cert-ref>",
    )
    bundle.app = AppInfo(
        project=project,
        source=SourceInfo(
            repo="https://github.com/acme/repo",
            ref="main",
            commit_sha="c0mm1t5ha",
            tree_hash=tree_hash,
            tree_hash_kind="git",
        ),
        image_digest="sha256:imagedigest",
        binding_quote={"pubkey": app_pubkey, "signature_chain": ["<sig-chain-root>"]},
        operator_debug=OperatorDebugInfo(enabled=True, last_session_at=""),
    )
    bundle.audit = [{"action": "promote", "timestamp": 1234567890}]
    return bundle


def test_bundle_roundtrip():
    """Every field the producer sets is populated on the consumer side.

    This is the drift gate: build a bundle through the producer path, serialize
    it (what the daemon serves), parse it via ``verify_from_bundle``, and assert
    each producer field lands on a consumer ``Facts`` field. If a future schema
    bump adds a producer field with no consumer home, this test fails instead of
    dropping it silently.
    """
    wire = _full_producer_bundle().to_dict()  # producer serialization
    facts = asyncio.run(verify_from_bundle(wire))

    # schema_version is pinned and shared (a bump must be deliberate)
    assert facts.schema_version == SCHEMA_VERSION == wire["schema_version"]
    assert facts.attestation_kind == "daemon-vouched"

    # platform_quote -> QuoteFacts (+ binding.report_data)
    assert facts.quote.quote_raw == "<raw-platform-quote>"
    assert facts.quote.report_data == wire["platform_quote"]["report_data"]

    # webhost_app_id
    assert facts.app_id == "<webhost-app-id>"

    # onchain (every producer field echoed; chain_id str-encoded on the consumer)
    assert facts.onchain.chain_id == "0"
    assert facts.onchain.kms_contract == "0xKMS"
    assert facts.onchain.dstackapp_address == "0xDAPP"
    assert facts.onchain.allowed_compose_hash == "0xCOMPOSE"
    assert facts.onchain.allowed_os_image == "0xOSIMAGE"

    # gateway -> ChannelFacts
    assert facts.channel.domain == "gateway.example"
    assert facts.channel.app_id == "<gateway-app-id>"
    assert facts.channel.zt_cert_ref == "<zt-cert-ref>"

    # app.project + app.image_digest (previously dropped — drift fix)
    assert facts.project == "rfc-roundtrip"
    assert facts.image_digest == "sha256:imagedigest"

    # app.source -> SourceFacts (incl. tree_hash_kind, previously dropped)
    assert facts.source.repo == "https://github.com/acme/repo"
    assert facts.source.ref == "main"
    assert facts.source.commit_sha == "c0mm1t5ha"
    assert facts.source.tree_hash == "deadbeef" * 8
    assert facts.source.tree_hash_kind == "git"

    # app.binding_quote -> BindingFacts (preimage verifies against the report_data)
    assert facts.binding.app_pubkey == "02abcdef"
    assert facts.binding.signature_chain_root == "<sig-chain-root>"
    assert facts.binding.preimage_verified is True

    # app.operator_debug -> OperatorDebugFacts (RFC 0029)
    assert facts.operator_debug.enabled is True
    assert facts.operator_debug.last_session_at == ""

    print("test_bundle_roundtrip: PASS (every producer field populated on consumer side)")


def test_bundle_to_dict_is_the_served_shape():
    """EvidenceBundle.to_dict() matches the shape the daemon serves (the committed
    fixture in test_daemon.py). Guards acceptance bullet 4: the daemon's
    ``/_api/verification/<project>`` response shape must not change."""
    wire = _full_producer_bundle().to_dict()
    expected_keys = {"schema_version", "platform_quote", "webhost_app_id",
                     "onchain", "gateway", "app", "audit"}
    assert expected_keys == set(wire.keys()), expected_keys ^ set(wire.keys())
    assert set(wire["app"]["source"]) == {"repo", "ref", "commit_sha", "tree_hash", "tree_hash_kind"}
    assert set(wire["onchain"]) == {"chain_id", "kms_contract", "dstackapp",
                                     "allowed_compose_hash", "allowed_os_image"}
    assert set(wire["gateway"]) == {"domain", "app_id", "zt_cert_ref"}
    assert wire["onchain"]["chain_id"] == 0
    print("test_bundle_to_dict_is_the_served_shape: PASS (response shape unchanged)")


class _RpcResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class _RpcSession:
    def __init__(self, responses):
        self.responses = iter(responses)

    def post(self, *args, **kwargs):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return _RpcResponse(response)


def _chain_config():
    return {
        "rpc_url": "https://base.example/rpc",
        "contract_address": "0x" + "aa" * 20,
        "compose_hash": "0x" + "11" * 32,
    }


def test_onchain_approval():
    from verify.facts import _verify_onchain_approval

    session = _RpcSession([
        {"result": "0x2105"},
        {"result": "0x" + "0" * 63 + "1"},
    ])
    facts = asyncio.run(_verify_onchain_approval(session, "0x" + "22" * 20, _chain_config()))
    assert facts.chain_id == "8453"
    assert facts.approved is True
    assert facts.error == ""
    assert facts.repro["method"].startswith("isAppAllowed(")


def test_onchain_not_approved():
    from verify.facts import _verify_onchain_approval

    session = _RpcSession([{"result": "0x2105"}, {"result": "0x" + "0" * 64}])
    facts = asyncio.run(_verify_onchain_approval(session, "0x" + "22" * 20, _chain_config()))
    assert facts.chain_id == "8453"
    assert facts.approved is False
    assert facts.error == ""


def test_onchain_non_anchored():
    from verify.facts import _verify_onchain_approval

    facts = asyncio.run(_verify_onchain_approval(None, "pha-prod-app"))
    assert facts.chain_id == "0"
    assert facts.approved is False
    assert facts.error == ""


def test_onchain_rpc_unreachable():
    from verify.facts import _verify_onchain_approval

    session = _RpcSession([aiohttp.ClientError("timeout")])
    facts = asyncio.run(_verify_onchain_approval(session, "0x" + "22" * 20, _chain_config()))
    assert facts.chain_id == "0"
    assert "Base-prod RPC check failed" in facts.error


def test_onchain_rpc_failure_flows_into_facts_errors():
    """Acceptance (#90): an unreachable base-prod RPC lands in
    OnchainFacts.error AND Facts.errors[] through the full verify core, which
    still returns Facts instead of raising."""
    from verify.facts import _verify_bundle

    session = _RpcSession([aiohttp.ClientError("timeout")])
    bundle = _load(GOOD)
    bundle["webhost_app_id"] = "0x" + "22" * 20  # fixture is non-anchored; give the check an app_id
    facts = asyncio.run(_verify_bundle(bundle, session=session, chain_config=_chain_config()))
    assert facts.onchain.chain_id == "0"
    assert "Base-prod RPC check failed" in facts.onchain.error
    assert any(e.startswith("onchain:") for e in facts.errors)


def test_qvl_report_is_parsed_and_missing_tool_is_specific():
    from verify.facts import _verify_quote_signature

    qvl_report = json.dumps({
        "status": "UpToDate",
        "report": {"TD10": {
            "mr_td": "aa" * 48,
            "rt_mr0": "bb" * 48,
            "rt_mr1": "cc" * 48,
            "rt_mr2": "dd" * 48,
            "rt_mr3": "ee" * 48,
            "report_data": "ff" * 64,
        }},
    })
    completed = subprocess.CompletedProcess(
        args=["dcap-qvl"], returncode=0, stdout=qvl_report, stderr=""
    )
    with patch("verify.facts.subprocess.run", return_value=completed):
        facts = _verify_quote_signature({"quote": "00"})
    assert facts.quote_valid is True
    assert facts.mrtd == "aa" * 48
    assert facts.rtmr["rtmr3"] == "ee" * 48
    assert facts.report_data == "ff" * 64

    with patch("verify.facts.subprocess.run", side_effect=FileNotFoundError):
        facts = _verify_quote_signature({"quote": "00"})
    assert facts.quote_valid is False
    assert facts.verification_error == "dcap-qvl is not installed or not on PATH"


def test_garbage_quote_returns_fact_error():
    from verify.facts import _verify_quote_signature

    facts = _verify_quote_signature({"quote": "not-a-quote"})
    assert facts.quote_valid is False
    assert "not valid hexadecimal" in facts.verification_error


if __name__ == "__main__":
    test_tampered_tree_hash_surfaces_mismatch_and_verify_returns()
    test_non_anchored_ecosystem_surfaces_chain_id_zero_no_crash()
    test_two_policies_disagree_on_same_facts()
    test_allowlist_policy_catches_tampered_tree_hash()
    test_policies_are_ordinary_functions_not_on_facts()
    test_facts_exposes_no_verdict_field()
    test_report_data_preimage_is_sensitive_to_tree_hash()
    test_parse_invalid_bundle_raises_and_verify_surfaces_it()
    test_facts_to_dict_is_json_serializable_and_round_trips()
    test_bundle_roundtrip()
    test_bundle_to_dict_is_the_served_shape()
    test_onchain_approval()
    test_onchain_not_approved()
    test_onchain_non_anchored()
    test_onchain_rpc_unreachable()
    test_onchain_rpc_failure_flows_into_facts_errors()
    test_qvl_report_is_parsed_and_missing_tool_is_specific()
    test_garbage_quote_returns_fact_error()
    print("\n=== ALL FACTS TESTS PASSED ===")

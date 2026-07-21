"""
Facts library for TEE attestation verification (RFC 0020/0025).

    verify(endpoint)          -> await Facts   # fetch + verify a live bundle over HTTP
    verify_from_bundle(dict)  -> await Facts   # verify a pre-fetched bundle dict

Both return structured Facts about what's running in a TEE. The library renders
NO verdict and shows no green/red — the accept-or-reject policy lives entirely
in the consumer (see verify/policies.py for two reference policies). Every
problem lands in Facts.errors[]; verify() never throws a verdict.

The bundle schema is defined ONCE in :mod:`verify.bundle`; this module parses
it via :meth:`EvidenceBundle.from_dict` and reads typed attributes, so a field
renamed or dropped on one side of the wire surfaces here rather than silently
emptying out. The round-trip test (``test_bundle_roundtrip``) locks that
contract.

The library is honest about what it can and cannot verify:
- The TDX quote is parsed but DCAP/QVL signature verification requires external
  tooling (Intel PCS / Phala verifier, tracked separately as #93); quote_valid
  stays False until that lands — never claim a verdict we did not earn.
- On non-anchored ecosystems (chain_id 0, e.g. pha-prod), onchain_approved is
  surfaced as False — a FACT, not an error.
- The RFC 0025 report-data binding is recomputed from the bundle's claimed
  (app_id, project, tree_hash, app_pubkey) and compared to the quote's
  report_data; an empty/zero or non-matching report_data is recorded in
  errors[] as a tree_hash binding failure.
- The RFC 0029 declared operator-debug door is surfaced as a fact (enabled /
  last_session_at), never a verdict.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

import aiohttp

from .bundle import (
    SCHEMA_VERSION,
    EvidenceBundle,
    OnchainInfo,
    OperatorDebugInfo,
    SourceInfo,
)

log = logging.getLogger(__name__)

# Domain separator for RFC 0025 report-data binding preimages.
REPORT_DATA_DOMAIN = b"tee-daemon/app-attest/v1"


@dataclass
class ChannelFacts:
    """Gateway channel attestation facts."""
    domain: str = ""
    app_id: str = ""
    zt_cert_ref: str = ""
    error: str = ""


@dataclass
class BindingFacts:
    """Per-app binding quote facts (RFC 0025)."""
    kind: Literal["report-data-quote", "app-quote", "none"] = "none"
    binding_quote: str = ""
    report_data: str = ""
    preimage_verified: bool = False
    app_pubkey: str = ""
    signature_chain_root: str = ""
    promote_event_rtmr: int = 0
    promote_event_digest: str = ""
    error: str = ""


@dataclass
class QuoteFacts:
    """TDX platform quote facts."""
    quote_valid: bool = False
    quote_format: str = ""
    mrtd: str = ""
    rtmr: dict[str, str] = field(default_factory=dict)
    collateral_url: str = ""
    verification_error: str = ""
    quote_raw: str = ""


@dataclass
class OnchainFacts:
    """On-chain approval facts."""
    chain_id: str = "0"
    chain_name: str = ""
    kms_contract: str = ""
    dstackapp_address: str = ""
    allowed_compose_hash: str = ""
    allowed_os_image: str = ""
    approved: bool = False
    error: str = ""


@dataclass
class SourceFacts:
    """Source code location facts."""
    repo: str = ""
    ref: str = ""
    commit_sha: str = ""
    tree_hash: str = ""
    tree_hash_kind: str = "git"
    github_tree_match: bool = False
    github_tree_sha: str = ""
    error: str = ""


@dataclass
class OperatorDebugFacts:
    """RFC 0029 declared operator-debug door facts (a fact, not a verdict)."""
    enabled: bool = False
    last_session_at: str = ""  # null until Half B opens audited sessions


@dataclass
class Facts:
    """
    Complete attestation facts for a TEE-hosted app.

    This is the output of verify() — a structured collection of facts about
    what's running. No verdict, no accept/reject. The consumer decides policy
    based on these facts (see verify/policies.py).

    Every field the producer sets on the :class:`~verify.bundle.EvidenceBundle`
    has a home here; :func:`verify_from_bundle` populates them. If a future
    schema bump adds a producer field with no consumer home, the round-trip
    test fails rather than dropping it silently.
    """
    schema_version: str = SCHEMA_VERSION
    channel: ChannelFacts = field(default_factory=ChannelFacts)
    app_id: str = ""
    attestation_kind: Literal["daemon-vouched", "app-cvm", "unknown"] = "unknown"
    project: str = ""
    image_digest: str = ""
    operator_debug: OperatorDebugFacts = field(default_factory=OperatorDebugFacts)
    quote: QuoteFacts = field(default_factory=QuoteFacts)
    binding: BindingFacts = field(default_factory=BindingFacts)
    onchain: OnchainFacts = field(default_factory=OnchainFacts)
    source: SourceFacts = field(default_factory=SourceFacts)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "channel": {
                "domain": self.channel.domain,
                "app_id": self.channel.app_id,
                "zt_cert_ref": self.channel.zt_cert_ref,
                "error": self.channel.error,
            },
            "app_id": self.app_id,
            "attestation_kind": self.attestation_kind,
            "project": self.project,
            "image_digest": self.image_digest,
            "operator_debug": {
                "enabled": self.operator_debug.enabled,
                "last_session_at": self.operator_debug.last_session_at,
            },
            "quote": {
                "quote_valid": self.quote.quote_valid,
                "quote_format": self.quote.quote_format,
                "mrtd": self.quote.mrtd,
                "rtmr": self.quote.rtmr,
                "collateral_url": self.quote.collateral_url,
                "verification_error": self.quote.verification_error,
            },
            "binding": {
                "kind": self.binding.kind,
                "report_data": self.binding.report_data,
                "preimage_verified": self.binding.preimage_verified,
                "app_pubkey": self.binding.app_pubkey,
                "signature_chain_root": self.binding.signature_chain_root,
                "promote_event_rtmr": self.binding.promote_event_rtmr,
                "promote_event_digest": self.binding.promote_event_digest,
                "error": self.binding.error,
            },
            "onchain": {
                "chain_id": self.onchain.chain_id,
                "chain_name": self.onchain.chain_name,
                "kms_contract": self.onchain.kms_contract,
                "dstackapp_address": self.onchain.dstackapp_address,
                "allowed_compose_hash": self.onchain.allowed_compose_hash,
                "allowed_os_image": self.onchain.allowed_os_image,
                "approved": self.onchain.approved,
                "error": self.onchain.error,
            },
            "source": {
                "repo": self.source.repo,
                "ref": self.source.ref,
                "commit_sha": self.source.commit_sha,
                "tree_hash": self.source.tree_hash,
                "tree_hash_kind": self.source.tree_hash_kind,
                "github_tree_match": self.source.github_tree_match,
                "github_tree_sha": self.source.github_tree_sha,
                "error": self.source.error,
            },
            "errors": self.errors,
        }

    def is_valid(self) -> bool:
        """
        Check if all verifications passed (errors list is empty).

        This is a CONVENIENCE method for simple consumers, NOT the library's
        verdict. A consumer may legitimately accept facts whose errors[] is
        non-empty (e.g. an allowlist policy on a staging bundle whose quote
        binding is unverifiable). Policy lives in the consumer.
        """
        return len(self.errors) == 0


class BundleParseError(Exception):
    """Raised when the bundle cannot be parsed."""


class BundleFetchError(Exception):
    """Raised when the bundle cannot be fetched."""


# --- bundle parsing ---------------------------------------------------------

def _parse_bundle(bundle_data: dict) -> EvidenceBundle:
    """
    Parse an RFC 0020 evidence bundle via the single shared schema definition.

    Returns an :class:`EvidenceBundle`. Raises BundleParseError on any
    structural problem — never silently returns an empty bundle, because that
    is exactly how a verification leg could stop checking without anyone
    noticing.
    """
    if not isinstance(bundle_data, dict):
        raise BundleParseError("Bundle must be a JSON object")
    try:
        return EvidenceBundle.from_dict(bundle_data)
    except (TypeError, ValueError) as e:
        raise BundleParseError(f"Bundle structure invalid: {e}") from e


# --- RFC 0025 report-data binding preimage ----------------------------------

def _compute_report_data_preimage(
    app_id: str,
    name: str,
    tree_hash: str,
    app_pubkey: str,
    domain: bytes = REPORT_DATA_DOMAIN,
) -> str:
    """
    Compute the report_data preimage the daemon's quote must commit to.

    report_data = SHA-512(domain || app_id || name || tree_hash || app_pubkey)

    A daemon-vouched quote binds the project's tree_hash this way (RFC 0025); a
    consumer re-runs this and compares to the quote's report_data to detect a
    tampered tree_hash.
    """
    preimage = (
        domain
        + app_id.encode()
        + name.encode()
        + tree_hash.encode()
        + bytes.fromhex(app_pubkey)
    )
    return hashlib.sha512(preimage).hexdigest()


# --- verification legs ------------------------------------------------------

def _verify_quote_signature(platform_quote: dict) -> QuoteFacts:
    """
    Extract TDX quote fields. Full DCAP/QVL signature verification requires
    external tooling (Intel PCS collateral / Phala verifier) and is NOT
    performed here — quote_valid stays False and the limitation is surfaced as
    a fact, never masked. Wiring real DCAP/QVL is tracked as #93.
    """
    facts = QuoteFacts()
    if not platform_quote:
        facts.verification_error = "No platform quote in bundle"
        return facts
    quote_blob = platform_quote.get("quote")
    if isinstance(quote_blob, str) and quote_blob:
        facts.quote_raw = quote_blob
        facts.quote_format = "tdx-legacy"
    facts.report_data = platform_quote.get("report_data", "") or ""
    facts.verification_error = "DCAP/QVL quote signature verification not performed"
    facts.quote_valid = False
    return facts


def _verify_binding_preimage(
    project: str,
    tree_hash: str,
    app_pubkey: str,
    app_id: str,
    platform_quote: dict,
) -> BindingFacts:
    """
    Verify the RFC 0025 report-data binding: recompute the preimage from the
    bundle's claimed (app_id, project, tree_hash, app_pubkey) and compare to
    the quote's report_data. An empty/zero report_data means the quote does
    not bind the tree_hash at all; a non-matching report_data means the
    bundle's claimed tree_hash differs from what the quote bound. Both surface
    as errors[]; neither throws.
    """
    facts = BindingFacts()
    facts.report_data = platform_quote.get("report_data", "") or ""
    if not (project and tree_hash and app_pubkey):
        facts.error = "missing project/tree_hash/app_pubkey for binding preimage"
        return facts
    facts.kind = "report-data-quote"
    facts.app_pubkey = app_pubkey
    rd = facts.report_data
    if not rd or set(rd.lower()) == {"0"}:
        facts.error = "quote report_data is empty/zero — tree_hash not bound by the quote"
        return facts
    expected = _compute_report_data_preimage(app_id, project, tree_hash, app_pubkey)
    facts.preimage_verified = (rd.lower() == expected.lower())
    if not facts.preimage_verified:
        facts.error = "report_data does not commit to the bundle's claimed tree_hash"
    return facts


def _extract_onchain_facts(onchain: OnchainInfo) -> OnchainFacts:
    """
    Surface on-chain approval as facts. chain_id 0 (non-anchored, e.g.
    pha-prod) is an expected state — approved=False with NO error. The real
    base-prod DstackApp allowlist RPC is tracked as #90; until it lands,
    approved stays False even on chain_id 8453 (honest, not masked).
    """
    facts = OnchainFacts()
    chain_id = onchain.chain_id
    facts.chain_id = str(chain_id)
    facts.kms_contract = onchain.kms_contract
    facts.dstackapp_address = onchain.dstackapp
    facts.allowed_compose_hash = onchain.allowed_compose_hash
    facts.allowed_os_image = onchain.allowed_os_image
    if chain_id == 8453:
        facts.chain_name = "base-prod"
    elif chain_id == 0:
        facts.chain_name = "non-anchored"
    else:
        facts.chain_name = f"chain-{chain_id}"
    facts.approved = False  # real allowlist check is #90; never claim unearned approval
    if chain_id not in (0, 8453):
        facts.error = f"unexpected chain_id {chain_id}; no policy to evaluate"
    return facts


def _extract_source_facts(source: SourceInfo) -> SourceFacts:
    facts = SourceFacts()
    facts.repo = source.repo
    facts.ref = source.ref
    facts.commit_sha = source.commit_sha
    facts.tree_hash = source.tree_hash
    facts.tree_hash_kind = source.tree_hash_kind
    return facts


def _extract_operator_debug(od: OperatorDebugInfo) -> OperatorDebugFacts:
    """RFC 0029: surface the declared operator-debug door as a fact, no verdict."""
    return OperatorDebugFacts(enabled=od.enabled, last_session_at=od.last_session_at)


def _signature_chain_root(binding_quote: dict) -> str:
    """Best-effort root identifier from a binding quote's signature_chain.
    Entries are hex strings on real dstack bundles; older fixture shapes used
    dicts with an 'authority' field."""
    chain = binding_quote.get("signature_chain")
    if not isinstance(chain, list) or not chain:
        return ""
    first = chain[0]
    if isinstance(first, dict):
        return str(first.get("authority", ""))
    return str(first)


def _verify_bundle(bundle_data: dict, base_url: str = "") -> Facts:
    """Shared verification core for verify() and verify_from_bundle().

    Every producer field on the EvidenceBundle is mapped to a Facts home here;
    reading typed attributes (not string keys) means a renamed dataclass field
    is a compile-time-checked change, not a silent empty string.
    """
    facts = Facts()
    try:
        bundle = _parse_bundle(bundle_data)
    except BundleParseError as e:
        facts.errors.append(f"bundle parse: {e}")
        return facts

    facts.schema_version = bundle.schema_version
    facts.app_id = bundle.webhost_app_id
    facts.attestation_kind = "daemon-vouched"  # shared-daemon model; per-app CVM is RFC 0019

    # channel (gateway) facts
    facts.channel.domain = bundle.gateway.domain
    facts.channel.app_id = bundle.gateway.app_id
    facts.channel.zt_cert_ref = bundle.gateway.zt_cert_ref

    # app-level facts (project / image_digest were previously dropped — drift fix)
    facts.project = bundle.app.project
    facts.image_digest = bundle.app.image_digest

    # source facts
    facts.source = _extract_source_facts(bundle.app.source)
    if facts.source.error:
        facts.errors.append(f"source: {facts.source.error}")

    # RFC 0029 operator-debug door — a fact, not a verdict
    facts.operator_debug = _extract_operator_debug(bundle.app.operator_debug)

    # quote leg
    facts.quote = _verify_quote_signature(bundle.platform_quote)
    if facts.quote.verification_error:
        facts.errors.append(f"quote: {facts.quote.verification_error}")

    # binding leg (RFC 0025 report-data preimage)
    binding_quote = bundle.app.binding_quote
    app_pubkey = binding_quote.get("pubkey", "") or ""
    facts.binding = _verify_binding_preimage(
        project=bundle.app.project,
        tree_hash=facts.source.tree_hash,
        app_pubkey=app_pubkey,
        app_id=facts.app_id,
        platform_quote=bundle.platform_quote,
    )
    facts.binding.app_pubkey = app_pubkey
    facts.binding.signature_chain_root = _signature_chain_root(binding_quote)
    if facts.binding.error:
        facts.errors.append(f"binding: {facts.binding.error}")

    # onchain leg — facts; chain_id 0 is not an error
    facts.onchain = _extract_onchain_facts(bundle.onchain)
    if facts.onchain.error:
        facts.errors.append(f"onchain: {facts.onchain.error}")

    # gateway/channel leg — zt_cert_ref must be cited (verification of it is #93's class of work)
    if not facts.channel.zt_cert_ref:
        facts.errors.append("gateway: no zt_cert_ref — channel attestation not present")

    # audit sanity — a bundle with no promote event has no binding trail
    if not any(isinstance(e, dict) and e.get("action") == "promote" for e in bundle.audit):
        facts.errors.append("audit: no promote event recorded")

    if base_url:
        facts.channel.domain = facts.channel.domain or urlparse(base_url).netloc

    return facts


# --- public verify() --------------------------------------------------------

async def verify(
    endpoint: str,
    session: aiohttp.ClientSession | None = None,
    chain_config: dict | None = None,
) -> Facts:
    """
    Fetch an RFC 0020 evidence bundle from `endpoint` and verify it.

    Args:
        endpoint: Full verification URL (/_api/verification/<project>).
        session: Optional aiohttp session (created if not provided).
        chain_config: Reserved for the base-prod allowlist RPC (#90); unused
            until that lands.

    Returns:
        Facts. Raises BundleFetchError on transport failure, BundleParseError
        on a structurally invalid endpoint URL — but never raises a verdict.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        base_url = endpoint.rstrip("/")
        if "/_api/verification/" not in base_url:
            raise BundleParseError(
                "Endpoint must be a full verification URL (/_api/verification/<project>)"
            )
        async with session.get(base_url, headers={"Accept": "application/json"}) as resp:
            if resp.status != 200:
                raise BundleFetchError(f"HTTP {resp.status}: {await resp.text()}")
            bundle_data = await resp.json()
        return _verify_bundle(bundle_data, base_url=base_url)
    finally:
        if own_session:
            await session.close()


async def verify_from_bundle(bundle_data: dict, chain_config: dict | None = None) -> Facts:
    """
    Verify a pre-fetched RFC 0020 bundle dict (no HTTP). Useful when the
    consumer already has the bundle — e.g. it was handed to an agent
    out-of-band — and just needs the Facts extracted.

    Args:
        bundle_data: Verification bundle JSON.
        chain_config: Reserved for the base-prod allowlist RPC (#90); unused.

    Returns:
        Facts. A structurally invalid bundle surfaces in Facts.errors[] rather
        than raising — the library never throws a verdict.
    """
    return _verify_bundle(bundle_data, base_url="")

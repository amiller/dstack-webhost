"""
Facts library for TEE attestation verification.

Per RFC 0020/0025: verify(bundle) -> Facts returns structured facts about
what's running in a TEE. Renders NO verdict — policy lives in the consumer.

Facts include:
- channel: gateway attestation info
- app_id: dstack application identifier
- binding: report-data quote binding (RFC 0025)
- attestation_kind: "daemon-vouched" | "app-cvm"
- onchain_approved: whether app_id is on-chain-anchored
- errors: list of verification failures

The library is honest about what it can and cannot verify:
- On staging (pha-prod), quotes verify but onchain_approved=false (chain_id=0)
- Full DCAP/QVL verification requires external tooling; facts surface limitations
- MRTD/RTMR measurements are surfaced; comparison to approved compose is consumer policy
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urljoin

import aiohttp

log = logging.getLogger(__name__)


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

    This is the output of verify() — a structured collection of facts
    about what's running. No verdict, no accept/reject. The consumer
    decides policy based on these facts.

    Attributes:
        schema_version: Bundle schema version
        channel: Gateway channel attestation
        app_id: dstack application identifier
        attestation_kind: "daemon-vouched" | "app-cvm"
        quote: TDX quote verification facts
        binding: Per-app binding quote facts
        onchain: On-chain approval facts
        source: Source code location facts
        errors: List of verification errors (non-empty = some check failed)
    """
    schema_version: str = "1.0"
    channel: ChannelFacts = field(default_factory=ChannelFacts)
    app_id: str = ""
    attestation_kind: Literal["daemon-vouched", "app-cvm", "unknown"] = "unknown"
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
                "github_tree_match": self.source.github_tree_match,
                "github_tree_sha": self.source.github_tree_sha,
                "error": self.source.error,
            },
            "errors": self.errors,
        }

    def is_valid(self) -> bool:
        """
        Check if all verifications passed (errors list is empty).

        This is a CONVENIENCE method for simple policies, not the library's
        verdict. A consumer may have a different policy (e.g., accept even
        if onchain_approved=false on staging, or require specific tree_hash).
        """
        return len(self.errors) == 0


class BundleParseError(Exception):
    """Raised when the bundle cannot be parsed."""
    pass


class BundleFetchError(Exception):
    """Raised when the bundle cannot be fetched."""
    pass


def _parse_bundle(bundle_data: dict) -> tuple:
    """
    Parse bundle into constituent parts.

    Returns:
        (project_data, quote_data, audit_log)

    Raises:
        BundleParseError: If bundle structure is invalid
    """
    if not isinstance(bundle_data, dict):
        raise BundleParseError("Bundle must be a JSON object")

    project = bundle_data.get("project", {})
    quote = bundle_data.get("quote") or {}
    audit = bundle_data.get("audit") or []

    if not isinstance(project, dict):
        raise BundleParseError("project must be a JSON object")
    if not isinstance(audit, list):
        raise BundleParseError("audit must be a JSON array")

    return project, quote, audit


def _compute_report_data_preimage(
    app_id: str,
    name: str,
    tree_hash: str,
    app_pubkey: str,
    domain: bytes = b"tee-daemon/app-attest/v1",
) -> str:
    """
    Compute the report_data preimage hash (RFC 0025).

    The report_data field in a TDX quote carries a SHA-512 hash of:
        domain || app_id || name || tree_hash || app_pubkey

    Args:
        app_id: dstack application identifier
        name: Project name
        tree_hash: Source tree hash
        app_pubkey: App's compressed public key (hex string)
        domain: Domain separator for binding quotes

    Returns:
        Hex-encoded SHA-512 hash (64 bytes)
    """
    preimage = domain + app_id.encode() + name.encode() + tree_hash.encode() + bytes.fromhex(app_pubkey)
    return hashlib.sha512(preimage).hexdigest()


def _verify_quote_signature(quote_data: dict) -> QuoteFacts:
    """
    Verify TDX quote signature and extract measurements.

    This is a SKELETON implementation. Full DCAP/QVL verification requires
    external tooling (Intel DCAP verifier, Phala's verification service).

    Current implementation:
    - Checks if quote-like fields exist
    - Extracts available measurement data
    - Reports limitations honestly in facts

    Args:
        quote_data: Quote data from bundle (GetKey response)

    Returns:
        QuoteFacts with verification results
    """
    facts = QuoteFacts()

    if not quote_data or not isinstance(quote_data, dict):
        facts.verification_error = "No quote data in bundle"
        return facts

    # Check for signature_chain (KMS-rooted attestation)
    sig_chain = quote_data.get("signature_chain", [])
    if sig_chain and isinstance(sig_chain, list) and len(sig_chain) > 0:
        facts.signature_chain_root = sig_chain[0].get("authority", "unknown")
        facts.quote_format = "dstack-kms"

    # Check for pubkey (app's derived key)
    if "pubkey" in quote_data:
        facts.app_pubkey = quote_data["pubkey"]

    # Check for raw quote bytes (if available)
    if "quote" in quote_data and isinstance(quote_data["quote"], str):
        facts.quote_raw = quote_data["quote"]

    # Check for quote report data (if pre-parsed)
    if "report_data" in quote_data:
        facts.report_data = quote_data["report_data"]

    # Skeleton: we cannot verify DCAP/QVL without external tooling
    # This would require Intel's DCAP verifier or Phala's verification service
    facts.verification_error = "Full DCAP/QVL verification requires external tooling"
    facts.quote_valid = False  # Honest: not verified without tooling

    return facts


def _verify_binding_preimage(
    project: dict,
    quote_data: dict,
) -> BindingFacts:
    """
    Verify report_data binding (RFC 0025).

    For daemon-vouched attestation, the daemon generates a quote with
    report_data = SHA-512(domain || app_id || name || tree_hash || app_pubkey).

    This function recomputes the preimage hash and checks if it matches
    the quote's report_data.

    Args:
        project: Project metadata from bundle
        quote_data: Quote data from bundle

    Returns:
        BindingFacts with verification results
    """
    facts = BindingFacts()

    if not quote_data or not isinstance(quote_data, dict):
        facts.error = "No quote data for binding verification"
        return facts

    name = project.get("name", "")
    tree_hash = project.get("tree_hash", "")
    app_pubkey = quote_data.get("pubkey", "")

    if not all([name, tree_hash, app_pubkey]):
        facts.error = "Missing required fields for binding verification"
        return facts

    # For daemon-vouched, we'd need the app_id from platform metadata
    # For now, we can't fully verify without the quote's report_data
    # Mark as daemon-vouched but note limitations
    facts.kind = "report-data-quote"
    facts.app_pubkey = app_pubkey

    sig_chain = quote_data.get("signature_chain", [])
    if sig_chain and isinstance(sig_chain, list) and len(sig_chain) > 0:
        facts.signature_chain_root = sig_chain[0].get("authority", "unknown")

    # Skeleton: full verification requires the actual report_data from the quote
    facts.error = "report-data preimage verification requires quote report_data field"

    return facts


async def _verify_onchain_approval(
    session: aiohttp.ClientSession,
    app_id: str,
    chain_config: dict | None = None,
) -> OnchainFacts:
    """
    Check if app_id is on-chain-approved on base-prod.

    This requires calling the base-prod RPC to check the DstackApp allowlist.
    On staging (pha-prod), chain_id=0 and apps are not on-chain-anchored.

    Args:
        session: HTTP session for RPC calls
        app_id: dstack application identifier
        chain_config: Optional chain RPC config (endpoint, contract address)

    Returns:
        OnchainFacts with approval status
    """
    facts = OnchainFacts()

    if not app_id:
        facts.error = "No app_id provided for on-chain verification"
        return facts

    # Default to staging behavior: chain_id=0, not approved
    # This is honest about the current state
    facts.chain_id = "0"
    facts.chain_name = "unknown"
    facts.approved = False
    facts.error = "On-chain approval check requires base-prod RPC configuration"

    return facts


async def _verify_source_tree(
    session: aiohttp.ClientSession,
    project: dict,
) -> SourceFacts:
    """
    Verify source tree hash against GitHub.

    Checks that the tree_hash in the bundle matches what GitHub reports
    for the pinned commit.

    Args:
        session: HTTP session
        project: Project metadata from bundle

    Returns:
        SourceFacts with verification results
    """
    facts = SourceFacts()

    source = project.get("source", "")
    commit_sha = project.get("commit_sha", "")
    tree_hash = project.get("tree_hash", "")

    facts.repo = source
    facts.ref = project.get("ref", "")
    facts.commit_sha = commit_sha
    facts.tree_hash = tree_hash

    if not all([source, commit_sha, tree_hash]):
        facts.error = "Missing source repo, commit_sha, or tree_hash"
        return facts

    # Check if this is a GitHub repo
    if "github.com" not in source:
        facts.error = "Source is not a GitHub repo; cannot verify tree hash"
        return facts

    # Parse owner/repo from GitHub URL
    import re
    m = re.match(r"github\.com[:/]([^/]+)/([^/#]+?)(\.git)?$", source.replace("https://", "").replace("http://", ""))
    if not m:
        facts.error = f"Could not parse GitHub repo from {source}"
        return facts

    owner, repo = m.group(1), m.group(2)

    try:
        # Fetch commit from GitHub API
        url = f"https://api.github.com/repos/{owner}/{repo}/git/commits/{commit_sha}"
        async with session.get(url, headers={"Accept": "application/vnd.github.v3+json"}) as resp:
            if resp.status == 200:
                gh_data = await resp.json()
                gh_tree = gh_data.get("tree", {}).get("sha", "")
                facts.github_tree_sha = gh_tree
                facts.github_tree_match = (gh_tree == tree_hash)

                if not facts.github_tree_match:
                    facts.error = f"GitHub tree hash {gh_tree[:16]} does not match bundle {tree_hash[:16]}"
            elif resp.status == 404:
                facts.error = f"GitHub does not have commit {commit_sha[:8]} in {owner}/{repo}"
            else:
                facts.error = f"GitHub API returned {resp.status}"
    except Exception as e:
        facts.error = f"GitHub fetch failed: {e}"

    return facts


async def verify(
    endpoint: str,
    session: aiohttp.ClientSession | None = None,
    chain_config: dict | None = None,
) -> Facts:
    """
    Verify a TEE attestation bundle and return Facts.

    Args:
        endpoint: Verification endpoint URL or bundle URL
                  (e.g., "https://cvm/_api/verification/project" or just the base URL)
        session: Optional HTTP session (created if not provided)
        chain_config: Optional on-chain verification config

    Returns:
        Facts: Structured facts about the attestation

    Raises:
        BundleFetchError: If bundle cannot be fetched
        BundleParseError: If bundle structure is invalid
    """
    facts = Facts()

    # Create session if not provided
    own_session = False
    if session is None:
        session = aiohttp.ClientSession()
        own_session = True

    try:
        # Normalize URL and construct verification endpoint
        base_url = endpoint.rstrip("/")

        # If endpoint looks like a base URL, extract project name or list projects
        # For now, assume endpoint is the full verification URL
        if "/_api/verification/" not in base_url:
            # This might be a base URL; we'd need to discover projects
            # For now, error
            facts.errors.append("Endpoint must be a full verification URL (/_api/verification/<project>)")
            return facts

        # Fetch the bundle
        async with session.get(base_url, headers={"Accept": "application/json"}) as resp:
            if resp.status != 200:
                raise BundleFetchError(f"HTTP {resp.status}: {await resp.text()}")
            bundle_data = await resp.json()

        # Parse the bundle
        project, quote_data, audit_log = _parse_bundle(bundle_data)

        # Extract channel/domain facts
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        facts.channel.domain = parsed.netloc

        # Extract app_id (from quote or project metadata)
        if quote_data and isinstance(quote_data, dict):
            sig_chain = quote_data.get("signature_chain", [])
            if sig_chain and isinstance(sig_chain, list) and len(sig_chain) > 0:
                facts.app_id = sig_chain[0].get("authority", "")

        # Extract source facts
        facts.source.repo = project.get("source", "")
        facts.source.ref = project.get("ref", "")
        facts.source.commit_sha = project.get("commit_sha", "")
        facts.source.tree_hash = project.get("tree_hash", "")

        # Determine attestation_kind (default to daemon-vouched for shared daemon)
        # RFC 0025: app-cvm would have a different quote structure
        facts.attestation_kind = "daemon-vouched"

        # RFC 0029: surface the declared operator-debug door as a fact (no verdict).
        app_block = bundle_data.get("app", {}) or {}
        od = app_block.get("operator_debug") or {}
        facts.operator_debug.enabled = bool(od.get("enabled", False))
        facts.operator_debug.last_session_at = od.get("last_session_at", "") or ""

        # Verify quote
        facts.quote = _verify_quote_signature(quote_data)
        if facts.quote.verification_error:
            facts.errors.append(f"quote verification: {facts.quote.verification_error}")

        # Verify binding preimage
        facts.binding = _verify_binding_preimage(project, quote_data)
        if facts.binding.error:
            facts.errors.append(f"binding verification: {facts.binding.error}")

        # Verify on-chain approval
        facts.onchain = await _verify_onchain_approval(session, facts.app_id, chain_config)
        if facts.onchain.error:
            facts.errors.append(f"on-chain verification: {facts.onchain.error}")

        # Verify source tree
        facts.source = await _verify_source_tree(session, project)
        if facts.source.error:
            facts.errors.append(f"source verification: {facts.source.error}")

        # Collect any errors from audit log (optional check)
        if audit_log:
            # Check for promote event
            promote_events = [e for e in audit_log if e.get("action") == "promote"]
            if not promote_events:
                facts.errors.append("No promote event found in audit log")

    except BundleFetchError:
        raise
    except BundleParseError:
        raise
    except Exception as e:
        facts.errors.append(f"Unexpected error: {e}")
        log.exception("Unexpected error during verification")
    finally:
        if own_session:
            await session.close()

    return facts


async def verify_from_bundle(bundle_data: dict, chain_config: dict | None = None) -> Facts:
    """
    Verify from a pre-fetched bundle (skips HTTP fetch).

    Useful when the consumer already has the bundle and just needs
    to extract facts.

    Args:
        bundle_data: Verification bundle JSON
        chain_config: Optional on-chain verification config

    Returns:
        Facts: Structured facts about the attestation
    """
    facts = Facts()

    try:
        project, quote_data, audit_log = _parse_bundle(bundle_data)

        # Extract basic facts
        facts.source.repo = project.get("source", "")
        facts.source.ref = project.get("ref", "")
        facts.source.commit_sha = project.get("commit_sha", "")
        facts.source.tree_hash = project.get("tree_hash", "")

        # Extract app_id
        if quote_data and isinstance(quote_data, dict):
            sig_chain = quote_data.get("signature_chain", [])
            if sig_chain and isinstance(sig_chain, list) and len(sig_chain) > 0:
                facts.app_id = sig_chain[0].get("authority", "")
            facts.binding.app_pubkey = quote_data.get("pubkey", "")

        facts.attestation_kind = "daemon-vouched"

        # RFC 0029: surface the declared operator-debug door as a fact (no verdict).
        app_block = bundle_data.get("app", {}) or {}
        od = app_block.get("operator_debug") or {}
        facts.operator_debug.enabled = bool(od.get("enabled", False))
        facts.operator_debug.last_session_at = od.get("last_session_at", "") or ""

        # Verify quote
        facts.quote = _verify_quote_signature(quote_data)
        if facts.quote.verification_error:
            facts.errors.append(facts.quote.verification_error)

        # Verify binding
        facts.binding = _verify_binding_preimage(project, quote_data)
        if facts.binding.error:
            facts.errors.append(facts.binding.error)

        # Note: on-chain and source verification require HTTP, skipped here

    except BundleParseError as e:
        facts.errors.append(f"Bundle parse error: {e}")

    return facts

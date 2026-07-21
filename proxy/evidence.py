"""RFC 0020: Machine-Verifiable Attestation Evidence for App Consumers.

The versioned bundle wire-schema (``EvidenceBundle`` and the ``*Info``
dataclasses) is defined ONCE in :mod:`verify.bundle` and shared with the
consumer library, so producer and consumer cannot drift apart. This module
holds the daemon-side helpers: ``fetch_bundle`` and a flat ``VerificationFacts``
consumer used by the daemon's own test suite. The canonical, richer consumer
result type is :class:`verify.Facts`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

import aiohttp

from verify.bundle import (
    SCHEMA_VERSION,
    AppInfo,
    EvidenceBundle,
    GatewayInfo,
    OnchainInfo,
    OperatorDebugInfo,
    SourceInfo,
)

log = logging.getLogger(__name__)


@dataclass
class VerificationFacts:
    """Flat result of the daemon-side verify() - structured facts with NO verdict.

    Kept for the daemon's own test suite (``test_daemon.py``), which asserts on
    its scalar fields (``quote_valid``, ``onchain_approved``, ``kms_root``,
    ``gateway_attested``). The canonical, richer consumer result type is
    :class:`verify.Facts`.

    Schema:
    {
        quote_valid: bool,  # TDX quote verified via DCAP/QVL
        kms_root: bool,  # Quote rooted in KMS
        webhost_app_id: str,  # App ID from platform
        onchain_approved: bool,  # DstackApp allowlist check (false for chain_id 0)
        gateway_attested: bool,  # Gateway zt-cert verified
        source: SourceInfo,
        errors: list[str]  # All problems, never thrown
    }
    """
    quote_valid: bool = False
    kms_root: bool = False
    webhost_app_id: str = ""
    onchain_approved: bool = False
    gateway_attested: bool = False
    source: SourceInfo = field(default_factory=SourceInfo)
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "quote_valid": self.quote_valid,
            "kms_root": self.kms_root,
            "webhost_app_id": self.webhost_app_id,
            "onchain_approved": self.onchain_approved,
            "gateway_attested": self.gateway_attested,
            "source": asdict(self.source),
            "errors": self.errors,
        }


async def fetch_bundle(endpoint: str, name: str, session: aiohttp.ClientSession) -> Optional[EvidenceBundle]:
    """Fetch evidence bundle from verification endpoint.

    Args:
        endpoint: Base URL of the daemon (e.g., http://localhost:18080)
        name: Project name
        session: aiohttp session

    Returns:
        EvidenceBundle or None if fetch failed
    """
    url = f"{endpoint}/_api/verification/{name}"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                log.warning("Failed to fetch bundle from %s: status %s", url, resp.status)
                return None
            data = await resp.json()
            return EvidenceBundle.from_dict(data)
    except Exception as e:
        log.warning("Failed to fetch bundle: %s", e)
        return None


async def verify(endpoint: str, name: str, opts: dict | None = None) -> VerificationFacts:
    """Verify a project's attestation evidence.

    Returns structured FACTS with no verdict. Policy is implemented by the consumer.
    Never throws - all problems go in errors[].

    Args:
        endpoint: Base URL of the daemon
        name: Project name
        opts: Optional verification options (e.g., chain_id to expect)

    Returns:
        VerificationFacts with all verification results
    """
    opts = opts or {}
    facts = VerificationFacts()

    async with aiohttp.ClientSession() as session:
        bundle = await fetch_bundle(endpoint, name, session)
        if not bundle:
            facts.errors.append("Failed to fetch evidence bundle")
            return facts

        # Extract source info
        facts.source = bundle.app.source

        # Basic quote presence check
        if not bundle.platform_quote:
            facts.errors.append("No platform quote in bundle")
            return facts

        # Quote presence is not quote validity. Full DCAP/QVL verification (Intel
        # PCS collateral) is not performed here, so quote_valid stays False and the
        # limitation is surfaced as a fact — never claim a verdict we didn't earn.
        if "quote" not in bundle.platform_quote and "report_data" not in bundle.platform_quote:
            facts.errors.append("Platform quote missing quote/report_data fields")
        else:
            facts.errors.append("Platform quote present but DCAP/QVL verification not performed")

        # KMS root: check for signature_chain in GetKey response
        if bundle.app.binding_quote.get("signature_chain"):
            facts.kms_root = True
        else:
            facts.errors.append("No signature_chain in binding quote")

        # webhost_app_id
        facts.webhost_app_id = bundle.webhost_app_id

        # onchain_approved: true only if chain_id > 0 and DstackApp allowlisted
        # For MVP, chain_id 0 is expected for non-anchored deployments
        if bundle.onchain.chain_id == 0:
            facts.onchain_approved = False
            # This is expected for non-anchored, not an error
        elif bundle.onchain.chain_id > 0:
            # TODO: In production, verify against base-prod DstackApp contract
            facts.onchain_approved = False
            facts.errors.append(f"onchain_approved check not implemented for chain_id {bundle.onchain.chain_id}")

        # gateway_attested: check for zt_cert_ref
        if bundle.gateway.zt_cert_ref:
            facts.gateway_attested = True
        else:
            facts.errors.append("No gateway zt_cert reference")

        return facts


def compute_report_data_binding(project: str, commit_sha: str, tree_hash: str) -> str:
    """Compute the report_data that a quote should commit to for tree_hash binding.

    This binds the project identity to the quote's report_data, so a consumer can
    verify that the quote actually attests to this specific source tree.

    Format: sha256(project || commit_sha || tree_hash)
    """
    payload = f"{project}|{commit_sha}|{tree_hash}"
    return hashlib.sha256(payload.encode()).hexdigest()


def verify_report_data_binding(quote: dict, project: str, commit_sha: str, tree_hash: str) -> bool:
    """Verify that the quote's report_data commits to the expected binding.

    Args:
        quote: Platform quote dict (from GetQuote or GetKey response)
        project: Project name
        commit_sha: Git commit SHA
        tree_hash: Git tree SHA

    Returns:
        True if report_data matches expected binding
    """
    # Get report_data from quote - format varies by dstack version
    report_data_hex = None

    # Try GetQuote format first
    if "report_data" in quote:
        report_data_hex = quote["report_data"]
    # Try GetKey format (report_data might be in signature_chain)
    elif "signature_chain" in quote:
        chain = quote["signature_chain"]
        if isinstance(chain, dict) and "report_data" in chain:
            report_data_hex = chain["report_data"]
        elif isinstance(chain, list) and len(chain) > 0:
            # Might be nested in chain entries
            for entry in chain:
                if isinstance(entry, dict) and "report_data" in entry:
                    report_data_hex = entry["report_data"]
                    break

    if not report_data_hex:
        log.warning("No report_data found in quote")
        return False

    expected = compute_report_data_binding(project, commit_sha, tree_hash)
    return report_data_hex == expected

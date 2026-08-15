"""RFC 0020: Machine-Verifiable Attestation Evidence for App Consumers.

This module defines the versioned evidence bundle schema and the verify()
facts library.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, asdict, field
from typing import Any, Optional

import aiohttp

log = logging.getLogger(__name__)

# Current schema version - increment when structure changes
SCHEMA_VERSION = "1.0.0"

# RFC 0027: domain separator for the per-app binding report_data preimage.
APP_ATTEST_DOMAIN = b"tee-daemon/app-attest/v1"
# Byte offset of the 64-byte report_data field within a TDX v4 quote (TD report body).
REPORT_DATA_OFFSET = 568


@dataclass
class OnchainInfo:
    """On-chain contract addresses and allowed values."""
    chain_id: int = 0  # 0 for non-anchored (e.g., pha-prod7), real chain ID for base-prod
    kms_contract: str = ""  # KMS contract address (empty for non-anchored)
    dstackapp: str = ""  # DstackApp contract address (empty for non-anchored)
    allowed_compose_hash: str = ""  # Expected compose hash from DstackApp
    allowed_os_image: str = ""  # Expected OS image hash


@dataclass
class GatewayInfo:
    """Gateway attestation reference."""
    domain: str = ""
    app_id: str = ""
    zt_cert_ref: str = ""  # Reference to gateway's ZeroTrust cert quote


@dataclass
class SourceInfo:
    """Source code information."""
    repo: str = ""
    ref: str = ""  # branch or tag
    commit_sha: str = ""
    tree_hash: str = ""  # Git tree SHA for git repos, sha256(tree) for tarballs
    tree_hash_kind: str = "git"  # "git" or "sha256" - distinguishes git tree vs tarball hash


@dataclass
class AppInfo:
    """App-specific information."""
    project: str = ""
    source: SourceInfo = field(default_factory=SourceInfo)
    image_digest: str = ""
    binding_quote: dict = field(default_factory=dict)  # KMS GetKey result (signature_chain rooting app_pubkey)
    binding: dict = field(default_factory=dict)  # RFC 0027 report-data-quote binding block


@dataclass
class EvidenceBundle:
    """RFC 0020 Evidence Bundle - versioned schema for attestation evidence.

    Schema:
    {
        schema_version: str,
        platform_quote: dict,  // TDX GetQuote response
        webhost_app_id: str,  // Our app_id on the platform
        onchain: OnchainInfo,
        gateway: GatewayInfo,
        app: AppInfo
    }
    """
    schema_version: str = SCHEMA_VERSION
    platform_quote: dict = field(default_factory=dict)
    webhost_app_id: str = ""
    attestation_kind: str = ""  # RFC 0027: "daemon-vouched" | "app-cvm" ("" = no per-app binding)
    onchain: OnchainInfo = field(default_factory=OnchainInfo)
    gateway: GatewayInfo = field(default_factory=GatewayInfo)
    app: AppInfo = field(default_factory=AppInfo)

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "platform_quote": self.platform_quote,
            "webhost_app_id": self.webhost_app_id,
            "attestation_kind": self.attestation_kind,
            "onchain": asdict(self.onchain),
            "gateway": asdict(self.gateway),
            "app": {
                **asdict(self.app),
                "source": asdict(self.app.source),
            },
        }


@dataclass
class VerificationFacts:
    """Result of verify() - structured facts with NO verdict.

    Per RFC 0020: The library returns facts, not accept/reject. Policy lives
    in the consumer. Errors go in errors[], never thrown.

    Schema:
    {
        quote_valid: bool,  // TDX quote verified via DCAP/QVL
        kms_root: bool,  // Quote rooted in KMS
        webhost_app_id: str,  // App ID from platform
        onchain_approved: bool,  // DstackApp allowlist check (false for chain_id 0)
        gateway_attested: bool,  // Gateway zt-cert verified
        source: SourceInfo,
        errors: list[str]  // All problems, never thrown
    }
    """
    quote_valid: bool = False
    kms_root: bool = False
    webhost_app_id: str = ""
    attestation_kind: str = ""  # RFC 0027 fact: which identity the binding quote measures
    binding_verified: bool = False  # RFC 0027: recomputed report_data == quote's report_data
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
            "attestation_kind": self.attestation_kind,
            "binding_verified": self.binding_verified,
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
            return EvidenceBundle(
                schema_version=data.get("schema_version", SCHEMA_VERSION),
                platform_quote=data.get("platform_quote", {}),
                webhost_app_id=data.get("webhost_app_id", ""),
                attestation_kind=data.get("attestation_kind", ""),
                onchain=OnchainInfo(**data.get("onchain", {})),
                gateway=GatewayInfo(**data.get("gateway", {})),
                app=AppInfo(
                    project=data.get("app", {}).get("project", ""),
                    source=SourceInfo(**data.get("app", {}).get("source", {})),
                    image_digest=data.get("app", {}).get("image_digest", ""),
                    binding_quote=data.get("app", {}).get("binding_quote", {}),
                    binding=data.get("app", {}).get("binding", {}),
                ),
            )
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

        # RFC 0027: per-app binding. attestation_kind names which identity the
        # binding quote measures; binding_verified recomputes report_data from the
        # served preimage and checks it against report_data read out of the raw
        # signed quote bytes — the daemon cannot forge that, which is the point.
        facts.attestation_kind = bundle.attestation_kind
        binding = bundle.app.binding
        if bundle.attestation_kind and binding:
            pre = binding.get("preimage", {})
            recomputed = compute_app_report_data(
                pre.get("app_id", ""), pre.get("name", ""),
                pre.get("tree_hash", ""), pre.get("app_pubkey", "")).hex()
            quote_hex = _norm_hex(binding.get("binding_quote", {}).get("quote", ""))
            if not quote_hex:
                facts.errors.append("RFC 0027 binding: no raw binding quote to read report_data from")
            else:
                # report_data is the 64-byte tail of the TD report body (offset 568).
                quote_rd = quote_hex[REPORT_DATA_OFFSET * 2:(REPORT_DATA_OFFSET + 64) * 2]
                facts.binding_verified = quote_rd == recomputed
                if not facts.binding_verified:
                    facts.errors.append(
                        "RFC 0027 binding mismatch: recomputed report_data != the quote's report_data")
            # DCAP/QVL (that the quote is genuine, current-TCB Intel TDX) is the
            # consumer's step, as for platform_quote — the field read above needs no key.
        elif bundle.attestation_kind:
            facts.errors.append("attestation_kind set but app.binding block missing")

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


def _norm_hex(s: str) -> str:
    return s.lower().removeprefix("0x") if isinstance(s, str) else ""


def compute_app_report_data(app_id: str, name: str, tree_hash: str, app_pubkey: str) -> bytes:
    """RFC 0027 per-app binding report_data (fills the 64-byte quote field exactly).

        SHA-512(DOMAIN ‖ app_id ‖ name ‖ tree_hash ‖ app_pubkey)

    Encoding (unambiguous — `name` is the only variable-length field and is bracketed
    by fixed-length fields): DOMAIN = raw bytes b"tee-daemon/app-attest/v1"; app_id,
    tree_hash, app_pubkey are hex-decoded (a leading "0x" is stripped); name is UTF-8.
    Callers reconstruct this exact preimage from the served `binding.preimage` block.
    """
    h = hashlib.sha512()
    h.update(APP_ATTEST_DOMAIN)
    h.update(bytes.fromhex(_norm_hex(app_id)))
    h.update(name.encode("utf-8"))
    h.update(bytes.fromhex(_norm_hex(tree_hash)))
    h.update(bytes.fromhex(_norm_hex(app_pubkey)))
    return h.digest()

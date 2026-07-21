"""
tee-daemon verify() Facts library.

Per RFC 0020/0025: A library that returns structured Facts about a TEE-hosted app,
rendering NO verdict. The accept/reject policy lives in the consumer.

Usage:
    import verify
    facts = await verify.verify("https://cvm/_api/verification/my-app")
    if facts.is_valid():
        # Consumer decides: is this good enough?
        if facts.onchain.approved and facts.source.github_tree_match:
            # My policy says yes
            pass
"""

from .bundle import (
    SCHEMA_VERSION,
    AppInfo,
    EvidenceBundle,
    GatewayInfo,
    OnchainInfo,
    OperatorDebugInfo,
    SourceInfo,
)
from .facts import (
    BindingFacts,
    BundleFetchError,
    BundleParseError,
    ChannelFacts,
    Facts,
    OnchainFacts,
    QuoteFacts,
    SourceFacts,
    verify,
    verify_from_bundle,
)

__all__ = [
    # Bundle wire schema (single definition, shared with proxy/)
    "SCHEMA_VERSION",
    "EvidenceBundle",
    "OnchainInfo",
    "GatewayInfo",
    "SourceInfo",
    "OperatorDebugInfo",
    "AppInfo",
    # Verify facts
    "Facts",
    "ChannelFacts",
    "BindingFacts",
    "QuoteFacts",
    "OnchainFacts",
    "SourceFacts",
    "BundleParseError",
    "BundleFetchError",
    "verify",
    "verify_from_bundle",
]

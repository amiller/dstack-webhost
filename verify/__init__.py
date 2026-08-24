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
# The wire schema is imported eagerly: it is pure dataclasses (stdlib only), and the DAEMON
# imports it to build bundles. The verification half is loaded lazily instead, so `verify.bundle`
# can be imported without pulling `facts` — which needs aiohttp today and shells out to dcap-qvl
# after #93. That keeps the producer's dependency surface to a schema. It matters because the
# daemon is ATTESTED: anything it imports becomes part of what is being attested, so the thing
# being verified must not depend on the verifier.
_LAZY = {
    "BindingFacts", "BundleFetchError", "BundleParseError", "ChannelFacts", "Facts",
    "OnchainFacts", "QuoteFacts", "SourceFacts", "verify", "verify_from_bundle",
}


def __getattr__(name):  # PEP 562
    if name in _LAZY:
        from . import facts
        return getattr(facts, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | _LAZY)

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

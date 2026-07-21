"""RFC 0020 Evidence Bundle — the single wire-schema definition.

The daemon (``proxy/``) builds bundles with these dataclasses and the
``verify()`` library parses them back out. Defining the schema in ONE place
means a field added on one side of the wire cannot be silently absent on the
other — the producer and consumer share the same dataclasses, and the
round-trip test (``verify/test_facts.py::test_bundle_roundtrip``) locks the
contract so a schema bump has to be deliberate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Current schema version - increment when the structure changes.
SCHEMA_VERSION = "1.0.0"


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
class OperatorDebugInfo:
    """RFC 0029 declared operator-debug door (a fact, not a verdict)."""
    enabled: bool = False
    last_session_at: str = ""  # null until Half B opens audited sessions


@dataclass
class AppInfo:
    """App-specific information."""
    project: str = ""
    source: SourceInfo = field(default_factory=SourceInfo)
    image_digest: str = ""
    binding_quote: dict = field(default_factory=dict)  # Daemon's promotion quote binding tree_hash
    operator_debug: OperatorDebugInfo = field(default_factory=OperatorDebugInfo)


@dataclass
class EvidenceBundle:
    """RFC 0020 Evidence Bundle - versioned schema for attestation evidence.

    On-wire shape produced by ``to_dict`` and consumed by ``from_dict``::

        {
            schema_version: str,
            platform_quote: dict,   # TDX GetQuote response (opaque)
            webhost_app_id: str,    # Our app_id on the platform
            onchain: OnchainInfo,
            gateway: GatewayInfo,
            app: AppInfo,
            audit: list,            # per-project audit log entries
        }
    """
    schema_version: str = SCHEMA_VERSION
    platform_quote: dict = field(default_factory=dict)
    webhost_app_id: str = ""
    onchain: OnchainInfo = field(default_factory=OnchainInfo)
    gateway: GatewayInfo = field(default_factory=GatewayInfo)
    app: AppInfo = field(default_factory=AppInfo)
    audit: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict (the on-wire form)."""
        return {
            "schema_version": self.schema_version,
            "platform_quote": self.platform_quote,
            "webhost_app_id": self.webhost_app_id,
            "onchain": asdict(self.onchain),
            "gateway": asdict(self.gateway),
            "app": {
                **asdict(self.app),
                "source": asdict(self.app.source),
            },
            "audit": self.audit,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceBundle":
        """Reconstruct a bundle from its on-wire dict form.

        Single parser shared by the daemon's ``fetch_bundle`` and the verify
        library, so a field the producer emits but the consumer ignores (or a
        renamed dataclass field) surfaces as an error rather than drifting
        silently to an empty string.
        """
        if not isinstance(data, dict):
            raise TypeError("bundle must be a JSON object")

        app = data.get("app") or {}
        if not isinstance(app, dict):
            raise TypeError("app must be a JSON object")
        src = app.get("source") or {}
        od = app.get("operator_debug")
        binding_quote = app.get("binding_quote")
        platform_quote = data.get("platform_quote")

        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            platform_quote=platform_quote if isinstance(platform_quote, dict) else {},
            webhost_app_id=data.get("webhost_app_id", ""),
            onchain=OnchainInfo(**(data.get("onchain") or {})),
            gateway=GatewayInfo(**(data.get("gateway") or {})),
            app=AppInfo(
                project=app.get("project", ""),
                source=SourceInfo(**src),
                image_digest=app.get("image_digest", ""),
                binding_quote=binding_quote if isinstance(binding_quote, dict) else {},
                operator_debug=OperatorDebugInfo(**(od if isinstance(od, dict) else {})),
            ),
            audit=data.get("audit") or [],
        )

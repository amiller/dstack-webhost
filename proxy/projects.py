"""Project model and disk-backed store."""

import json
import os
import shutil
from dataclasses import dataclass, asdict, field
from typing import List
from typing import Optional


@dataclass
class ListenConfig:
    port: int = 0
    protocol: str = "http"


@dataclass
class Project:
    name: str
    runtime: str
    entry: str
    port: int
    mode: str = "dev"
    public: bool = False  # visibility axis: listed for anonymous callers
    env: dict = None
    container_id: str = ""
    deployed_at: str = ""
    image_digest: str = ""
    source: str = ""
    ref: str = ""
    commit_sha: str = ""
    tree_hash: str = ""
    listen: Optional[ListenConfig] = None
    image: str = ""
    image_port: int = 0
    volumes: List[dict] = field(default_factory=list)
    isolation: str = "shared"
    env_passthrough: List[str] = field(default_factory=list)
    dstack_env: dict = field(default_factory=dict)
    oci_runtime: str = ""  # per-project OCI runtime, e.g. "runsc" (gVisor); falls back to CONTAINER_RUNTIME
    # Elevated container capabilities — honored ONLY for mode=="attested" projects (see
    # deploy gate), so the grant is always on the verifiable attested surface. Used e.g.
    # for an in-container OpenVPN sidecar (CAP_NET_ADMIN + /dev/net/tun).
    cap_add: List[str] = field(default_factory=list)
    devices: List[str] = field(default_factory=list)
    # RFC 0029: a declared, measured operator-debug door (full trust, audited). Honored
    # ONLY for mode=="attested" (see deploy gate), so the door is always on the verifiable
    # surface — its existence is part of the measurement, never a hidden side channel.
    operator_debug: bool = False
    # RFC 0025 per-app attestation fields
    app_id: str = ""  # TDX workload app_id (from TDX_WORKLOAD_ID env var or GetKey response)
    app_pubkey: str = ""  # KMS-derived per-path compressed public key (hex)
    binding_quote: str = ""  # TDX quote binding app_id/name/tree_hash/app_pubkey (hex)
    report_data: str = ""  # SHA-512 of preimage (64 bytes, hex)
    attestation_kind: str = ""  # "daemon-vouched" or "app-cvm"

    def __post_init__(self):
        if self.env is None:
            self.env = {}
        if self.mode not in ("dev", "attested"):
            self.mode = "dev"
        # Initialize listen config if not provided
        # port=0 means "path-based only, no dedicated port"
        if self.listen is None:
            self.listen = ListenConfig(port=0, protocol="http")


class ProjectStore:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _project_dir(self, name: str) -> str:
        return os.path.join(self.base_dir, name)

    def files_dir(self, name: str) -> str:
        return os.path.join(self._project_dir(name), "files")

    def save(self, project: Project):
        d = self._project_dir(project.name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "project.json"), "w") as f:
            json.dump(asdict(project), f)

    def load(self, name: str) -> Project:
        with open(os.path.join(self._project_dir(name), "project.json")) as f:
            data = json.load(f)
            # Convert listen dict to ListenConfig object if present
            if "listen" in data and data["listen"] is not None:
                data["listen"] = ListenConfig(**data["listen"])
            # Strip unknown fields (e.g. legacy "attested" boolean)
            valid = {f.name for f in Project.__dataclass_fields__.values()}
            data = {k: v for k, v in data.items() if k in valid}
            return Project(**data)

    def list(self) -> list[Project]:
        projects = []
        for name in sorted(os.listdir(self.base_dir)):
            p = os.path.join(self._project_dir(name), "project.json")
            if os.path.isfile(p):
                projects.append(self.load(name))
        return projects

    def delete(self, name: str):
        shutil.rmtree(self._project_dir(name))

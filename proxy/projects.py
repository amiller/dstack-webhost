"""Project model and disk-backed store."""

from __future__ import annotations

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
    oci_runtime: str = ""  # per-project OCI runtime, e.g. "runsc" (gVisor); falls back to CONTAINER_RUNTIME

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

    def export_bundle(self) -> dict:
        return {
            "version": 1,
            "projects": [export_project(p) for p in self.list()],
        }

    def import_manifests(self, bundle: dict) -> list[dict]:
        if not isinstance(bundle, dict):
            raise ValueError("import bundle must be a JSON object")
        if bundle.get("version") != 1:
            raise ValueError(f"unsupported import bundle version: {bundle.get('version')!r}")
        projects = bundle.get("projects")
        if not isinstance(projects, list):
            raise ValueError("import bundle must contain a projects list")
        return [import_manifest(p) for p in projects]


EXPORT_FIELDS = (
    "name", "runtime", "entry", "port", "mode", "image_digest", "source",
    "ref", "commit_sha", "tree_hash", "listen", "image", "image_port",
    "volumes", "isolation", "env_passthrough", "oci_runtime",
)


def export_project(project: Project) -> dict:
    data = asdict(project)
    return {k: data[k] for k in EXPORT_FIELDS if k in data}


def import_manifest(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("project import entry must be a JSON object")
    out = {k: data[k] for k in EXPORT_FIELDS if k in data}
    out.pop("env", None)
    if not out.get("name"):
        raise ValueError("project import entry missing name")
    if not out.get("runtime"):
        raise ValueError(f"project {out['name']!r} missing runtime")
    return out

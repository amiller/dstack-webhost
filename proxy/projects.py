"""Project model and disk-backed store."""

import json
import os
import shutil
from dataclasses import dataclass, asdict, field
from typing import Optional


@dataclass
class ListenConfig:
    port: int = 8080
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

    def __post_init__(self):
        if self.env is None:
            self.env = {}
        if self.mode not in ("dev", "attested"):
            self.mode = "dev"
        # Initialize listen config with defaults if not provided
        if self.listen is None:
            self.listen = ListenConfig(port=self.port, protocol="http")


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

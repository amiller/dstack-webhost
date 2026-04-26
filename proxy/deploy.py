"""Deploy and teardown logic — git clone or tarball upload."""

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import time
from datetime import datetime, timezone

from .docker_client import DockerClient
from .projects import Project, ProjectStore, ListenConfig
from .tracker import ContainerTracker
from .audit import AuditLog, AuditEntry
from .runtimes import RuntimeManager, RUNTIME_CONFIG, VOLUME_NAME, VOLUME_MOUNT

log = logging.getLogger(__name__)

NETWORK_DEV = "tee-apps-dev"
NETWORK_ATTESTED = "tee-apps-attested"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VALID_RUNTIMES = set(RUNTIME_CONFIG.keys()) | {"static", "dockerfile"}

DEFAULT_ENTRY = {
    "deno": "server.ts", "bun": "index.ts", "node": "index.js",
    "python": "app.py", "static": ".", "dockerfile": "Dockerfile",
}
DEFAULT_PORT = {
    "deno": 3000, "bun": 3000, "node": 3000,
    "python": 8000, "static": 8080, "dockerfile": 8080,
}

AUTODETECT = [
    ("server.ts", "deno"), ("index.ts", "bun"), ("index.js", "node"),
    ("app.py", "python"), ("index.html", "static"),
]

BUILD_STEPS = {
    "node": ("package.json", "npm install --production"),
    "python": ("requirements.txt", "pip install -r requirements.txt"),
    "deno": ("deno.json", "deno cache {entry}"),
}


async def git_clone(source: str, ref: str, dest: str) -> tuple[str, str]:
    """Clone source@ref to dest. Returns (commit_sha, git_tree_sha).

    The git_tree_sha is the SHA-1 of the commit's tree object — the same
    value GitHub exposes via /repos/<owner>/<repo>/git/commits/<sha>. A
    relying party can verify it without cloning, by querying the GitHub API.
    """
    if os.path.exists(dest):
        shutil.rmtree(dest)
    url = source if source.startswith(("https://", "http://", "/")) else f"https://{source}"
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, dest]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ValueError(f"git clone failed: {stderr.decode().strip()}")
    proc2 = await asyncio.create_subprocess_exec(
        "git", "-C", dest, "rev-parse", "HEAD",
        stdout=asyncio.subprocess.PIPE)
    stdout, _ = await proc2.communicate()
    commit_sha = stdout.decode().strip()
    proc3 = await asyncio.create_subprocess_exec(
        "git", "-C", dest, "rev-parse", "HEAD^{tree}",
        stdout=asyncio.subprocess.PIPE)
    stdout, _ = await proc3.communicate()
    git_tree_sha = stdout.decode().strip()
    return commit_sha, git_tree_sha


def extract_tarball(data: bytes, dest: str) -> None:
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in m.name.split("/"):
                raise ValueError(f"unsafe tar member: {m.name}")
        tf.extractall(dest, filter="data")


def compute_tree_hash(directory: str) -> str:
    h = hashlib.sha256()
    for root, dirs, files in sorted(os.walk(directory)):
        dirs[:] = [d for d in sorted(dirs) if d != ".git"]
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            relpath = os.path.relpath(fpath, directory)
            h.update(relpath.encode())
            h.update(b"\0")
            with open(fpath, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


def detect_manifest(files_dir: str) -> dict:
    pj = os.path.join(files_dir, "project.json")
    if os.path.isfile(pj):
        with open(pj) as f:
            return json.load(f)
    for fname, runtime in AUTODETECT:
        if os.path.isfile(os.path.join(files_dir, fname)):
            return {"runtime": runtime, "entry": fname}
    return {}


async def run_build_step(docker: DockerClient, runtime: str, entry: str, files_dir: str):
    build = BUILD_STEPS.get(runtime)
    if not build:
        return
    marker, cmd_template = build
    if not os.path.isfile(os.path.join(files_dir, marker)):
        return

    config = RUNTIME_CONFIG.get(runtime)
    if not config:
        return

    cmd_str = cmd_template.replace("{entry}", entry)
    image = config["image"]
    await docker.pull(image)

    if VOLUME_NAME:
        rel = os.path.relpath(files_dir, VOLUME_MOUNT)
        binds = [f"{VOLUME_NAME}:/daemon-vol"]
        workdir = f"/daemon-vol/{rel}"
    else:
        binds = [f"{os.path.abspath(files_dir)}:/app"]
        workdir = "/app"

    build_cmd = ["sh", "-c", f"cd {workdir} && {cmd_str}"]
    log.info("Building %s: %s", runtime, cmd_str)
    exit_code, logs = await docker.run_build(image, build_cmd, binds)
    if exit_code != 0:
        raise RuntimeError(f"Build failed (exit {exit_code}):\n{logs}")
    log.info("Build complete")


async def deploy(store: ProjectStore, docker: DockerClient, audit_manager,
                 tracker: ContainerTracker, rtm: RuntimeManager,
                 manifest: dict, files_data: bytes | None = None) -> Project:
    source = manifest.get("source", "")
    ref = manifest.get("ref", "")
    name = manifest.get("name", "")

    if not name or not NAME_RE.match(name):
        raise ValueError(f"Invalid project name: {name!r}")

    files_dir = store.files_dir(name)
    git_tree_sha = ""
    if files_data is not None:
        extract_tarball(files_data, files_dir)
        commit_sha = manifest.get("commit_sha", "")
    else:
        if not source:
            raise ValueError("Missing source (provide git source or upload tarball via multipart)")
        commit_sha, git_tree_sha = await git_clone(source, ref, files_dir)

    repo_manifest = detect_manifest(files_dir)

    runtime = manifest.get("runtime") or repo_manifest.get("runtime", "")
    entry = manifest.get("entry") or repo_manifest.get("entry") or DEFAULT_ENTRY.get(runtime, "")
    port = int(manifest.get("port", 0)) or int(repo_manifest.get("port", 0)) or DEFAULT_PORT.get(runtime, 0)
    mode = manifest.get("mode") or repo_manifest.get("mode", "dev")
    if mode not in ("dev", "attested"):
        mode = "dev"
    env_vars = {**repo_manifest.get("env", {}), **manifest.get("env", {})}

    # Parse listen configuration with defaults
    listen_manifest = manifest.get("listen") or repo_manifest.get("listen")
    if listen_manifest is None:
        # Default listen config: use detected port or fallback to 8080/http
        listen_port = port or 8080
        listen_protocol = "http"
    else:
        listen_port = int(listen_manifest.get("port", port)) or 8080
        listen_protocol = listen_manifest.get("protocol", "http") or "http"
    listen_config = ListenConfig(port=listen_port, protocol=listen_protocol)

    # Check for port conflicts with existing projects
    # Port 8080 is special: multiple projects can use it for path-based routing
    if listen_port != 8080:
        existing_projects = store.list()
        for existing in existing_projects:
            if existing.listen and existing.listen.port == listen_port:
                # Allow redeploying the same project on the same port
                if existing.name != name:
                    raise ValueError(
                        f"Port conflict: project '{name}' cannot bind to port {listen_port} "
                        f"because it is already in use by project '{existing.name}'"
                    )

    if not runtime:
        raise ValueError("Cannot detect runtime — add project.json or specify runtime")
    if runtime not in VALID_RUNTIMES:
        raise ValueError(f"Unknown runtime: {runtime!r}")

    # For git-cloned deploys use the commit's git tree SHA so a relying
    # party can verify it against the GitHub API. For tarball deploys,
    # fall back to a SHA-256 over the working tree.
    tree_hash = git_tree_sha or compute_tree_hash(files_dir)

    await run_build_step(docker, runtime, entry, files_dir)

    config = RUNTIME_CONFIG.get(runtime)
    image = config["image"] if config else runtime

    project = Project(
        name=name, runtime=runtime, entry=entry, port=port, mode=mode,
        env=env_vars, deployed_at=datetime.now(timezone.utc).isoformat(),
        source=source, ref=ref, commit_sha=commit_sha, tree_hash=tree_hash,
        listen=listen_config,
    )
    store.save(project)

    if runtime not in ("static", "dockerfile"):
        await rtm.refresh(runtime)

    digest = await docker.image_digest(image) if config else ""
    project.image_digest = digest
    store.save(project)

    # Only record audit log for attested mode
    if mode == "attested":
        audit = audit_manager.get_audit_log(name)
        await audit.record(AuditEntry(
            timestamp=time.time(), action="deploy", image=image, image_digest=digest,
            detail=json.dumps({"name": name, "source": source, "ref": ref,
                               "commit": commit_sha, "tree_hash": tree_hash})))

    log.info("Deployed %s from %s@%s (%s)", name, source, ref or "HEAD", commit_sha[:12])
    return project


async def teardown(store: ProjectStore, docker: DockerClient, audit_manager,
                   tracker: ContainerTracker, rtm: RuntimeManager, name: str):
    project = store.load(name)

    # Only record audit log for attested mode
    if project.mode == "attested":
        audit = audit_manager.get_audit_log(name)
        await audit.record(AuditEntry(
            timestamp=time.time(), action="teardown", detail=name,
            image_digest=project.image_digest))

    store.delete(name)

    if project.runtime not in ("static", "dockerfile"):
        await rtm.refresh(project.runtime)

    log.info("Torn down %s", name)


async def promote(store: ProjectStore, audit_manager, rtm: RuntimeManager,
                  name: str) -> Project:
    """Promote a project from dev mode to attested mode."""
    project = store.load(name)

    if project.mode == "attested":
        raise ValueError(f"Project {name} is already in attested mode")

    # Change mode to attested and save
    project.mode = "attested"
    store.save(project)

    # Record promotion in audit log with source hash (now attested)
    audit = audit_manager.get_audit_log(name)
    await audit.record(AuditEntry(
        timestamp=time.time(),
        action="promote",
        detail=json.dumps({
            "name": name,
            "from_mode": "dev",
            "to_mode": "attested",
            "source": project.source,
            "ref": project.ref,
            "commit": project.commit_sha,
            "tree_hash": project.tree_hash,
        }),
        image=project.image_digest,
        image_digest=project.image_digest,
    ))

    # Re-deploy on attested network
    if project.runtime not in ("static", "dockerfile"):
        await rtm.refresh(project.runtime)

    log.info("Promoted %s to attested mode (commit: %s, tree_hash: %s)",
             name, project.commit_sha[:12], project.tree_hash[:12])
    return project

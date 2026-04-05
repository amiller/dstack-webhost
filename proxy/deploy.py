"""Deploy and teardown logic — git-only deploys."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone

from .docker_client import DockerClient
from .projects import Project, ProjectStore
from .tracker import ContainerTracker
from .audit import AuditLog, AuditEntry
from .runtimes import RuntimeManager, RUNTIME_CONFIG, VOLUME_NAME, VOLUME_MOUNT

log = logging.getLogger(__name__)

NETWORK = "tee-apps"
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


async def git_clone(source: str, ref: str, dest: str) -> str:
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
    return stdout.decode().strip()


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


async def deploy(store: ProjectStore, docker: DockerClient, audit: AuditLog,
                 tracker: ContainerTracker, rtm: RuntimeManager,
                 manifest: dict) -> Project:
    source = manifest.get("source", "")
    ref = manifest.get("ref", "")
    name = manifest.get("name", "")

    if not source:
        raise ValueError("Missing source")
    if not name or not NAME_RE.match(name):
        raise ValueError(f"Invalid project name: {name!r}")

    files_dir = store.files_dir(name)
    commit_sha = await git_clone(source, ref, files_dir)

    repo_manifest = detect_manifest(files_dir)

    runtime = manifest.get("runtime") or repo_manifest.get("runtime", "")
    entry = manifest.get("entry") or repo_manifest.get("entry") or DEFAULT_ENTRY.get(runtime, "")
    port = int(manifest.get("port", 0)) or int(repo_manifest.get("port", 0)) or DEFAULT_PORT.get(runtime, 0)
    attested = manifest.get("attested", repo_manifest.get("attested", False))
    env_vars = {**repo_manifest.get("env", {}), **manifest.get("env", {})}

    if not runtime:
        raise ValueError("Cannot detect runtime — add project.json or specify runtime")
    if runtime not in VALID_RUNTIMES:
        raise ValueError(f"Unknown runtime: {runtime!r}")

    tree_hash = compute_tree_hash(files_dir)

    await run_build_step(docker, runtime, entry, files_dir)

    config = RUNTIME_CONFIG.get(runtime)
    image = config["image"] if config else runtime

    project = Project(
        name=name, runtime=runtime, entry=entry, port=port, attested=attested,
        env=env_vars, deployed_at=datetime.now(timezone.utc).isoformat(),
        source=source, ref=ref, commit_sha=commit_sha, tree_hash=tree_hash,
    )
    store.save(project)

    if runtime not in ("static", "dockerfile"):
        await rtm.refresh(runtime)

    digest = await docker.image_digest(image) if config else ""
    project.image_digest = digest
    store.save(project)

    await audit.record(AuditEntry(
        timestamp=time.time(), action="deploy", image=image, image_digest=digest,
        detail=json.dumps({"name": name, "source": source, "ref": ref,
                           "commit": commit_sha, "tree_hash": tree_hash})))

    log.info("Deployed %s from %s@%s (%s)", name, source, ref or "HEAD", commit_sha[:12])
    return project


async def teardown(store: ProjectStore, docker: DockerClient, audit: AuditLog,
                   tracker: ContainerTracker, rtm: RuntimeManager, name: str):
    project = store.load(name)

    await audit.record(AuditEntry(
        timestamp=time.time(), action="teardown", detail=name,
        image_digest=project.image_digest))

    store.delete(name)

    if project.runtime not in ("static", "dockerfile"):
        await rtm.refresh(project.runtime)

    log.info("Torn down %s", name)

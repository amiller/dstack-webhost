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

import aiohttp

from .docker_client import DockerClient
from .projects import Project, ProjectStore, ListenConfig
from .tracker import ContainerTracker
from .audit import AuditLog, AuditEntry
from .runtimes import RuntimeManager, RUNTIME_CONFIG, VOLUME_NAME, VOLUME_MOUNT
from . import secp, evidence

log = logging.getLogger(__name__)

NETWORK_DEV = "tee-apps-dev"
NETWORK_ATTESTED = "tee-apps-attested"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VALID_RUNTIMES = set(RUNTIME_CONFIG.keys()) | {"static", "dockerfile", "image"}

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

    if manifest.get("runtime") == "image":
        return await _deploy_image(store, audit_manager, rtm, manifest)

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
    # Elevated caps are honored ONLY for attested projects, so the grant is always on
    # the verifiable surface. Prefer the repo-committed manifest so tree_hash commits to
    # them (a verifier fetching the source commit can confirm what was granted).
    cap_add = repo_manifest.get("cap_add") or manifest.get("cap_add", []) or []
    devices = repo_manifest.get("devices") or manifest.get("devices", []) or []
    if (cap_add or devices) and mode != "attested":
        raise ValueError("cap_add/devices require mode=attested")
    env_vars = {**repo_manifest.get("env", {}), **manifest.get("env", {})}
    isolation = manifest.get("isolation") or repo_manifest.get("isolation", "shared")
    if isolation not in ("shared", "container"):
        isolation = "shared"

    # Parse listen configuration with defaults
    listen_manifest = manifest.get("listen") or repo_manifest.get("listen")
    if listen_manifest is None:
        # Default listen config. Shared-isolation projects are served via
        # path-based ingress on 8080; only container projects own a dedicated port.
        listen_port = 8080 if isolation == "shared" else (port or 8080)
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
        public=bool(manifest.get("public", False)),
        env=env_vars, deployed_at=datetime.now(timezone.utc).isoformat(),
        source=source, ref=ref, commit_sha=commit_sha, tree_hash=tree_hash,
        listen=listen_config, isolation=isolation,
        env_passthrough=manifest.get("env_passthrough") or repo_manifest.get("env_passthrough", []) or [],
        oci_runtime=manifest.get("oci_runtime", ""),
        cap_add=cap_add, devices=devices,
        egress=bool(manifest.get("egress", False)),
        egress_provider=bool(manifest.get("egress_provider", False)),
    )
    store.save(project)

    if isolation == "container" and runtime in ("deno", "bun"):
        digest = await rtm.start_isolated(project)
    else:
        if runtime not in ("static", "dockerfile"):
            await rtm.refresh(runtime)
        digest = await docker.image_digest(image) if config else ""
    project.image_digest = digest
    store.save(project)

    # Record every deploy — dev projects share the CVM with attested tenants,
    # so their mutations must be auditable too.
    audit = audit_manager.get_audit_log(name)
    await audit.record(AuditEntry(
        timestamp=time.time(), action="deploy", image=image, image_digest=digest,
        detail=json.dumps({"name": name, "mode": mode, "source": source, "ref": ref,
                           "commit": commit_sha, "tree_hash": tree_hash,
                           "cap_add": cap_add, "devices": devices})))

    log.info("Deployed %s from %s@%s (%s)", name, source, ref or "HEAD", commit_sha[:12])
    return project


async def _deploy_image(store: ProjectStore, audit_manager,
                        rtm: RuntimeManager, manifest: dict) -> Project:
    name = manifest["name"]
    image = manifest.get("image", "")
    image_port = int(manifest.get("image_port", 0))
    if not image:
        raise ValueError("runtime=image requires 'image' field")
    if not image_port:
        raise ValueError("runtime=image requires 'image_port' field")

    mode = manifest.get("mode", "dev")
    if mode not in ("dev", "attested"):
        mode = "dev"
    # Image projects have no source tree, so the caps are bound by the append-only audit
    # detail + the image @sha256 digest (immutable code), not tree_hash. Still attested-only.
    cap_add = manifest.get("cap_add", []) or []
    devices = manifest.get("devices", []) or []
    if (cap_add or devices) and mode != "attested":
        raise ValueError("cap_add/devices require mode=attested")
    env_vars = manifest.get("env", {})

    listen_manifest = manifest.get("listen") or {}
    listen_port = int(listen_manifest.get("port", 8080)) or 8080
    listen_protocol = listen_manifest.get("protocol", "http") or "http"
    listen_config = ListenConfig(port=listen_port, protocol=listen_protocol)

    if listen_port != 8080:
        for existing in store.list():
            if existing.name != name and existing.listen and existing.listen.port == listen_port:
                raise ValueError(
                    f"Port conflict: project '{name}' cannot bind to port {listen_port} "
                    f"because it is already in use by project '{existing.name}'")

    volumes = manifest.get("volumes", []) or []
    env_passthrough = manifest.get("env_passthrough", []) or []
    oci_runtime = manifest.get("oci_runtime", "")
    project = Project(
        name=name, runtime="image", entry="", port=0, mode=mode,
        public=bool(manifest.get("public", False)),
        env=env_vars, deployed_at=datetime.now(timezone.utc).isoformat(),
        source=manifest.get("source", ""), ref=manifest.get("ref", ""),
        commit_sha=manifest.get("commit_sha", ""), tree_hash=manifest.get("tree_hash", ""),
        image=image, image_port=image_port, volumes=volumes,
        env_passthrough=env_passthrough, listen=listen_config,
        oci_runtime=oci_runtime, cap_add=cap_add, devices=devices,
        egress=bool(manifest.get("egress", False)),
        egress_provider=bool(manifest.get("egress_provider", False)),
    )
    store.save(project)

    digest = await rtm.start_image(project)
    project.image_digest = digest
    store.save(project)

    audit = audit_manager.get_audit_log(name)
    await audit.record(AuditEntry(
        timestamp=time.time(), action="deploy", image=image, image_digest=digest,
        detail=json.dumps({"name": name, "image": image, "image_port": image_port,
                           "image_digest": digest, "cap_add": cap_add, "devices": devices})))

    log.info("Deployed image project %s from %s (digest %s)", name, image, digest[:19])
    return project


async def teardown(store: ProjectStore, docker: DockerClient, audit_manager,
                   tracker: ContainerTracker, rtm: RuntimeManager, name: str):
    project = store.load(name)

    audit = audit_manager.get_audit_log(name)
    await audit.record(AuditEntry(
        timestamp=time.time(), action="teardown", detail=name,
        image_digest=project.image_digest))

    store.delete(name)

    if project.runtime == "image":
        await rtm.stop_image(name)
    elif project.isolation == "container" and project.runtime in ("deno", "bun"):
        await rtm.stop_isolated(name)
    elif project.runtime not in ("static", "dockerfile"):
        await rtm.refresh(project.runtime)

    log.info("Torn down %s", name)


async def _dstack_post(sock: str, method: str, body: dict) -> dict:
    conn = aiohttp.UnixConnector(path=sock)
    async with aiohttp.ClientSession(connector=conn) as session:
        async with session.post(f"http://localhost/{method}", json=body) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"dstack {method} failed ({resp.status}): {data}")
            return data


def _app_id_from_eventlog(event_log: str) -> str:
    """The CVM's app-id, as measured into RTMR3 (event 'app-id', imr 3)."""
    for e in json.loads(event_log or "[]"):
        if e.get("imr") == 3 and e.get("event") == "app-id":
            return e.get("event_payload", "")
    return ""


async def build_app_binding(sock: str, name: str, tree_hash: str,
                            commit_sha: str, image_digest: str) -> dict:
    """RFC 0027 (b): produce a hardware-rooted per-app binding at promote time.

    Derives app_pubkey (GetKey), binds it plus the exact tree_hash into a fresh
    TDX quote's report_data (GetQuote), and lands the promotion in RTMR3's measured
    log (EmitEvent). Any RPC failure propagates — a swallowed error here would be a
    silent false "verified".
    """
    # app_id is the CVM's own measured identity — read it from the quote event log,
    # not the WEBHOST_APP_ID env (which is unreliable; empty on staging).
    probe = await _dstack_post(sock, "GetQuote", {"report_data": "00" * 64})
    app_id = _app_id_from_eventlog(probe.get("event_log", ""))
    if not app_id:
        raise RuntimeError("no app-id in the dstack quote event log; cannot build RFC 0027 binding")

    key_path = f"/tee-daemon/projects/{name}"
    getkey = await _dstack_post(sock, "GetKey", {"path": key_path})
    if "key" not in getkey:
        raise RuntimeError(f"GetKey returned no key for {key_path}")
    # Derive the compressed pubkey; never persist the private key.
    app_pubkey = secp.compressed_pubkey(bytes.fromhex(getkey["key"].replace("0x", ""))[:32]).hex()

    report_data = evidence.compute_app_report_data(app_id, name, tree_hash, app_pubkey).hex()
    binding_quote = await _dstack_post(sock, "GetQuote", {"report_data": report_data})

    payload = json.dumps({"name": name, "tree_hash": tree_hash,
                          "commit": commit_sha, "image_digest": image_digest},
                         sort_keys=True).encode()
    await _dstack_post(sock, "EmitEvent",
                       {"event": "tee-daemon/promote", "payload": payload.hex()})

    return {
        "kind": "report-data-quote",
        "binding_quote": binding_quote,
        "report_data": "0x" + report_data,
        "preimage": {
            "domain": evidence.APP_ATTEST_DOMAIN.decode(),
            "app_id": app_id,
            "name": name,
            "tree_hash": tree_hash,
            "app_pubkey": app_pubkey,
        },
        "app_pubkey": app_pubkey,
        "promote_event": {
            "rtmr": 3,
            "event": "tee-daemon/promote",
            "digest": hashlib.sha384(payload).hexdigest(),
        },
    }


async def promote(store: ProjectStore, audit_manager, rtm: RuntimeManager,
                  name: str, dstack_sock: str | None = None) -> Project:
    """Promote a project from dev mode to attested mode."""
    project = store.load(name)

    if project.mode == "attested":
        raise ValueError(f"Project {name} is already in attested mode")

    # Change mode to attested and save
    project.mode = "attested"
    store.save(project)

    # RFC 0027 (b): hardware-rooted per-app binding quote. Only when dstack is
    # present (inside the CVM); outside a TEE there is no quote to produce — the
    # same condition the verification/attest endpoints already gate on.
    if dstack_sock:
        project.binding = await build_app_binding(
            dstack_sock, name, project.tree_hash, project.commit_sha, project.image_digest)
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

    # Recreate so the container picks up attested-only settings (caps/devices, e.g. NET_ADMIN
    # + /dev/net/tun for the VPN egress). Per-project containers (image, isolation:container)
    # aren't touched by refresh() — they must be explicitly restarted in the new mode.
    if project.runtime == "image":
        await rtm.stop_image(project.name)
        await rtm.start_image(project)
    elif project.isolation == "container" and project.runtime in ("deno", "bun"):
        await rtm.stop_isolated(project.name)
        await rtm.start_isolated(project)
    elif project.runtime not in ("static", "dockerfile"):
        await rtm.refresh(project.runtime)

    log.info("Promoted %s to attested mode (commit: %s, tree_hash: %s)",
             name, project.commit_sha[:12], project.tree_hash[:12])
    return project

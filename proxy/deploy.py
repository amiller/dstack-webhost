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
from . import secp
from .tracker import ContainerTracker
from .audit import AuditLog, AuditEntry
from .runtimes import RuntimeManager, RUNTIME_CONFIG, VOLUME_NAME, VOLUME_MOUNT

log = logging.getLogger(__name__)

NETWORK_DEV = "tee-apps-dev"
NETWORK_ATTESTED = "tee-apps-attested"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VALID_RUNTIMES = set(RUNTIME_CONFIG.keys()) | {"static", "dockerfile", "image"}

# Sentinel ingress substitutes for every env value in project responses (issue
# #67). Nothing legitimately deploys it: a client POSTing it back is round-
# tripping a redacted GET and would overwrite real secrets (issue #132).
REDACTED = "<redacted>"

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


async def git_clone(source: str, ref: str, dest: str,
                    commit_sha: str = "") -> tuple[str, str]:
    """Clone source@ref to dest. Returns (commit_sha, git_tree_sha).

    The git_tree_sha is the SHA-1 of the commit's tree object — the same
    value GitHub exposes via /repos/<owner>/<repo>/git/commits/<sha>. A
    relying party can verify it without cloning, by querying the GitHub API.

    With commit_sha set (RFC 0017 pinned import), the full history is cloned
    and checked out at exactly that commit; a sha that no longer exists raises.
    """
    if os.path.exists(dest):
        shutil.rmtree(dest)
    url = source if source.startswith(("https://", "http://", "/")) else f"https://{source}"
    cmd = ["git", "clone"]
    if not commit_sha:
        cmd += ["--depth", "1"]
        if ref:
            cmd += ["--branch", ref]
    cmd += [url, dest]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ValueError(f"git clone failed: {stderr.decode().strip()}")
    if commit_sha:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", dest, "checkout", "--detach", commit_sha,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise ValueError(
                f"pinned commit {commit_sha} not found in {source}: "
                f"{stderr.decode().strip()}")
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


# RFC 0025: Per-app attestation helpers
APP_ATTEST_DOMAIN = b"tee-daemon/app-attest/v1"
DSTACK_SOCK = None  # Set by main.py


async def _dstack_getkey(project_name: str) -> dict | None:
    """Call dstack GetKey for a project and return the sanitized response.
    Returns dict with pubkey and signature_chain, or None on error."""
    if not DSTACK_SOCK:
        log.warning("dstack not available - cannot get app pubkey")
        return None
    key_path = f"/tee-daemon/projects/{project_name}"
    body = {"path": key_path}
    try:
        conn = aiohttp.UnixConnector(path=DSTACK_SOCK)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.post("http://localhost/GetKey", json=body) as resp:
                if resp.status != 200:
                    log.warning("GetKey failed: %s", resp.status)
                    return None
                data = await resp.json()
                # Sanitize: extract compressed pubkey, discard private key
                if not isinstance(data, dict) or "key" not in data:
                    log.warning("GetKey response missing 'key'")
                    return None
                priv = bytes.fromhex(data["key"].replace("0x", ""))[:32]
                out = {k: v for k, v in data.items() if k != "key"}
                out["pubkey"] = secp.compressed_pubkey(priv).hex()
                # Extract app_id if present in response
                out["app_id"] = data.get("app_id", "")
                return out
    except Exception as e:
        log.warning("GetKey failed: %s", e)
        return None


async def _dstack_getquote(report_data: bytes) -> str | None:
    """Call dstack GetQuote and return the quote bytes as hex, or None on error."""
    if not DSTACK_SOCK:
        log.warning("dstack not available - cannot get quote")
        return None
    if len(report_data) != 64:
        log.warning("report_data must be 64 bytes, got %d", len(report_data))
        return None
    body = {"report_data": report_data.hex()}
    try:
        conn = aiohttp.UnixConnector(path=DSTACK_SOCK)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.post("http://localhost/GetQuote", json=body) as resp:
                if resp.status != 200:
                    log.warning("GetQuote failed: %s", resp.status)
                    return None
                data = await resp.json()
                quote = data.get("quote", "")
                if not quote:
                    log.warning("GetQuote response missing 'quote'")
                    return None
                return quote
    except Exception as e:
        log.warning("GetQuote failed: %s", e)
        return None


async def _dstack_emit_event(event: str, payload: str) -> bool:
    """Call dstack EmitEvent to extend RTMR. Returns True on success."""
    if not DSTACK_SOCK:
        log.warning("dstack not available - cannot emit event")
        return False
    body = {"event": event, "payload": payload}
    try:
        conn = aiohttp.UnixConnector(path=DSTACK_SOCK)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.post("http://localhost/EmitEvent", json=body) as resp:
                if resp.status == 200:
                    log.info("RTMR event emitted: %s", event)
                    return True
                else:
                    log.warning("EmitEvent returned %s", resp.status)
                    return False
    except Exception as e:
        log.warning("EmitEvent failed: %s", e)
        return False


def compute_report_data(app_id: str, name: str, tree_hash: str, app_pubkey: str) -> bytes:
    """Compute SHA-512 report_data for per-app binding quote.
    SHA-512(APP_ATTEST_DOMAIN ‖ app_id ‖ name ‖ tree_hash ‖ app_pubkey)
    Returns 64 bytes.
    """
    h = hashlib.sha512()
    h.update(APP_ATTEST_DOMAIN)
    h.update(app_id.encode())
    h.update(b"\0")
    h.update(name.encode())
    h.update(b"\0")
    h.update(tree_hash.encode())
    h.update(b"\0")
    h.update(app_pubkey.encode())
    return h.digest()  # 64 bytes


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

    redacted_keys = sorted(
        k for k, v in (manifest.get("env") or {}).items() if v == REDACTED)
    if redacted_keys:
        raise ValueError(
            f"env keys {', '.join(redacted_keys)} carry the redaction sentinel "
            f"{REDACTED!r} — refusing to overwrite stored secrets with it")

    if manifest.get("runtime") == "image":
        return await _deploy_image(store, docker, audit_manager, rtm, manifest)

    files_dir = store.files_dir(name)
    git_tree_sha = ""
    if files_data is not None:
        extract_tarball(files_data, files_dir)
        commit_sha = manifest.get("commit_sha", "")
    else:
        if not source:
            raise ValueError("Missing source (provide git source or upload tarball via multipart)")
        pin_sha = manifest.get("commit_sha", "")
        pin_tree = manifest.get("tree_hash", "")
        if pin_sha:
            # RFC 0017 pinned import: clone and verify in a staging dir; the
            # live files_dir is only replaced once the tree matches the pin,
            # so a tampered bundle never clobbers a deployed project.
            stage = files_dir + ".pin"
            commit_sha, git_tree_sha = await git_clone(
                source, ref, stage, commit_sha=pin_sha)
            if pin_tree and git_tree_sha != pin_tree:
                shutil.rmtree(stage)
                raise ValueError(
                    f"tree_hash mismatch for {name}: pinned {pin_tree}, "
                    f"source at {pin_sha[:12]} hashes to {git_tree_sha}")
            if os.path.exists(files_dir):
                shutil.rmtree(files_dir)
            shutil.move(stage, files_dir)
        else:
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
    # operator_debug is a measured boolean (RFC 0029); prefer the repo-committed value so
    # tree_hash commits to it, mirroring cap_add. A declared door is full trust, attested-only.
    operator_debug = bool(repo_manifest.get("operator_debug", manifest.get("operator_debug", False)))
    if operator_debug and mode != "attested":
        raise ValueError("operator_debug requires mode=attested")
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
        description=repo_manifest.get("description", ""),
        listen=listen_config, isolation=isolation,
        env_passthrough=manifest.get("env_passthrough") or repo_manifest.get("env_passthrough", []) or [],
        dstack_env=manifest.get("dstack_env") or repo_manifest.get("dstack_env", {}) or {},
        oci_runtime=manifest.get("oci_runtime", ""),
        cap_add=cap_add, devices=devices, operator_debug=operator_debug,
    )
    store.save(project)

    await rtm.ensure_project_broker(name)

    if isolation == "container" and runtime in ("deno", "bun"):
        digest = await rtm.start_isolated(project)
    else:
        if runtime not in ("static", "dockerfile"):
            await rtm.refresh(runtime)
        digest = await docker.image_digest(image) if config else ""
    project.image_digest = digest
    store.save(project)

    # Record every deploy — dev projects share the CVM with attested apps,
    # so their mutations must be auditable too.
    audit = audit_manager.get_audit_log(name)
    await audit.record(AuditEntry(
        timestamp=time.time(), action="deploy", image=image, image_digest=digest,
        detail=json.dumps({"name": name, "mode": mode, "source": source, "ref": ref,
                           "commit": commit_sha, "tree_hash": tree_hash,
                           "cap_add": cap_add, "devices": devices,
                           "operator_debug": operator_debug})))

    log.info("Deployed %s from %s@%s (%s)", name, source, ref or "HEAD", commit_sha[:12])
    return project


async def _deploy_image(store: ProjectStore, docker: DockerClient,
                        audit_manager, rtm: RuntimeManager,
                        manifest: dict) -> Project:
    name = manifest["name"]
    image = manifest.get("image", "")
    image_port = int(manifest.get("image_port", 0))
    if not image:
        raise ValueError("runtime=image requires 'image' field")
    if not image_port:
        raise ValueError("runtime=image requires 'image_port' field")

    # RFC 0017 pinned import: the recorded image_digest is the pin. Verify it
    # against what the registry actually serves before anything is created — a
    # moved tag is an error, never silently served.
    pin_digest = manifest.get("image_digest", "")
    if pin_digest:
        if not await docker.image_digest(image):
            await docker.pull(image)
        got = await docker.image_digest(image)
        if got != pin_digest:
            raise ValueError(
                f"image_digest mismatch for {name}: pinned {pin_digest}, "
                f"registry has {got or 'unknown'}")

    mode = manifest.get("mode", "dev")
    if mode not in ("dev", "attested"):
        mode = "dev"
    # Image projects have no source tree, so the caps are bound by the append-only audit
    # detail + the image @sha256 digest (immutable code), not tree_hash. Still attested-only.
    cap_add = manifest.get("cap_add", []) or []
    devices = manifest.get("devices", []) or []
    if (cap_add or devices) and mode != "attested":
        raise ValueError("cap_add/devices require mode=attested")
    operator_debug = bool(manifest.get("operator_debug", False))
    if operator_debug and mode != "attested":
        raise ValueError("operator_debug requires mode=attested")
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
        description=manifest.get("description", ""),
        commit_sha=manifest.get("commit_sha", ""), tree_hash=manifest.get("tree_hash", ""),
        image=image, image_port=image_port, volumes=volumes,
        env_passthrough=env_passthrough, listen=listen_config,
        oci_runtime=oci_runtime, cap_add=cap_add, devices=devices,
        operator_debug=operator_debug,
    )
    store.save(project)

    await rtm.ensure_project_broker(name)

    digest = await rtm.start_image(project)
    project.image_digest = digest
    store.save(project)

    audit = audit_manager.get_audit_log(name)
    await audit.record(AuditEntry(
        timestamp=time.time(), action="deploy", image=image, image_digest=digest,
        detail=json.dumps({"name": name, "image": image, "image_port": image_port,
                           "image_digest": digest, "commit": manifest.get("commit_sha", ""),
                           "tree_hash": manifest.get("tree_hash", ""),
                           "mode": mode,
                           "cap_add": cap_add, "devices": devices,
                           "operator_debug": operator_debug})))

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

    await rtm.remove_project_broker(name)

    log.info("Torn down %s", name)


async def promote(store: ProjectStore, audit_manager, rtm: RuntimeManager,
                  name: str) -> Project:
    """Promote a project from dev mode to attested mode."""
    project = store.load(name)

    if project.mode == "attested":
        raise ValueError(f"Project {name} is already in attested mode")

    # RFC 0025 Phase 1: Generate per-app binding quote before finalizing
    # Get app_id from TDX_WORKLOAD_ID env var (present in TDX CVM)
    app_id = os.environ.get("TDX_WORKLOAD_ID", "")

    # Derive app_pubkey via GetKey
    key_data = await _dstack_getkey(name)
    if not key_data:
        log.warning("Failed to get app pubkey for %s - promotion without binding quote", name)
    else:
        app_pubkey = key_data.get("pubkey", "")
        if key_data.get("app_id"):
            app_id = key_data.get("app_id", "")

        # Compute report_data = SHA-512(domain ‖ app_id ‖ name ‖ tree_hash ‖ app_pubkey)
        report_data_bytes = compute_report_data(app_id, name, project.tree_hash, app_pubkey)
        report_data_hex = report_data_bytes.hex()

        # GetQuote for binding
        binding_quote = await _dstack_getquote(report_data_bytes)
        if binding_quote:
            # Emit promote event to RTMR3
            event_payload = json.dumps({
                "name": name,
                "tree_hash": project.tree_hash,
                "commit": project.commit_sha,
                "image_digest": project.image_digest,
            }, sort_keys=True)
            await _dstack_emit_event("tee-daemon/promote", event_payload.encode().hex())

            # Persist binding fields
            project.app_id = app_id
            project.app_pubkey = app_pubkey
            project.binding_quote = binding_quote
            project.report_data = report_data_hex
            project.attestation_kind = "daemon-vouched"
            log.info("Generated binding quote for %s (app_id=%s, tree_hash=%s)",
                     name, app_id[:20] if app_id else "-", project.tree_hash[:12])
        else:
            log.warning("Failed to get binding quote for %s", name)

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
            "attestation_kind": project.attestation_kind,
        }),
        image=project.image_digest,
        image_digest=project.image_digest,
    ))

    # Re-deploy on attested network
    if project.runtime not in ("static", "dockerfile"):
        await rtm.refresh(project.runtime)

    log.info("Promoted %s to attested mode (commit: %s, tree_hash: %s, kind: %s)",
             name, project.commit_sha[:12], project.tree_hash[:12],
             project.attestation_kind or "none")
    return project


async def unpromote(store: ProjectStore, audit_manager, rtm: RuntimeManager,
                    name: str) -> Project:
    """Return an attested project to dev mode and record the trust transition."""
    project = store.load(name)
    if project.mode != "attested":
        raise ValueError(f"Project {name} is already in dev mode")

    project.mode = "dev"
    store.save(project)

    audit = audit_manager.get_audit_log(name)
    await audit.record(AuditEntry(
        timestamp=time.time(),
        action="unpromote",
        detail=json.dumps({
            "name": name,
            "from_mode": "attested",
            "to_mode": "dev",
            "source": project.source,
            "ref": project.ref,
            "commit": project.commit_sha,
            "tree_hash": project.tree_hash,
            "image_digest": project.image_digest,
        }),
        image=project.image_digest,
        image_digest=project.image_digest,
    ))

    if project.runtime not in ("static", "dockerfile"):
        await rtm.refresh(project.runtime)
    return project


async def import_bundle(store: ProjectStore, docker: DockerClient, audit_manager,
                        tracker: ContainerTracker, rtm: RuntimeManager,
                        bundle: dict) -> dict:
    """RFC 0017 §2: redeploy every exported manifest *pinned*.

    Git projects clone at their recorded commit_sha and must reproduce the
    recorded tree_hash; image projects must match their recorded image_digest.
    A pin that cannot be reproduced errors and skips that project — there is
    no re-clone-latest fallback. A project that already exists is redeployed
    with its live env merged in (an export bundle carries no secrets; secret
    continuity is RFC 0018's job).
    """
    if not isinstance(bundle, dict) or not isinstance(bundle.get("projects"), list):
        raise ValueError("bundle must be an object with a 'projects' list")
    result = {"imported": [], "skipped": []}
    for entry in bundle["projects"]:
        name = str(entry.get("name", ""))
        manifest = dict(entry)
        try:
            manifest["env"] = dict(store.load(name).env or {})
        except FileNotFoundError:
            pass
        try:
            await deploy(store, docker, audit_manager, tracker, rtm, manifest)
        except ValueError as e:
            log.warning("import skipped %s: %s", name, e)
            result["skipped"].append({"project": name, "error": str(e)})
            continue
        result["imported"].append(name)
    return result

"""Shared runtime containers — one per runtime type, routes all projects."""

import json
import logging
import os

from .docker_client import DockerClient
from .projects import ProjectStore
from .tracker import ContainerTracker

log = logging.getLogger(__name__)

NETWORK_DEV = "tee-apps-dev"
NETWORK_ATTESTED = "tee-apps-attested"
# When running inside Docker, host paths don't work for sibling container bind mounts.
# Set DAEMON_VOLUME_NAME to the Docker volume name (e.g. "dstack_daemon_data")
# and DAEMON_VOLUME_MOUNT to the mount point inside the daemon (e.g. "/var/lib/tee-daemon").
# The runtime container will mount the volume directly.
VOLUME_NAME = os.environ.get("DAEMON_VOLUME_NAME", "")
VOLUME_MOUNT = os.environ.get("DAEMON_VOLUME_MOUNT", "/var/lib/tee-daemon")
# Optional writable per-project data volume. If set, the shared runtime gets
# it mounted at /daemon-data:rw and passes ctx.dataDir = /daemon-data/<name>
# to each handler. Projects use it for persistent state (DBs, subs, etc.).
DATA_VOLUME_NAME = os.environ.get("DAEMON_DATA_VOLUME_NAME", "")
DATA_VOLUME_MOUNT_IN_RUNTIME = "/daemon-data"
# Optional OCI runtime for daemon-managed containers (e.g. "sysbox-runc").
# Empty string keeps Docker's default (runc).
CONTAINER_RUNTIME = os.environ.get("DAEMON_CONTAINER_RUNTIME", "")

_ENTRY_SHIM_DENO = r"""
const [ENTRY, FILES, DATA, ENV_JSON] = Deno.args;
const env = JSON.parse(ENV_JSON || "{}");
const mod = await import(`file://${FILES}/${ENTRY}`);
const handler = mod.default;
Deno.serve({ port: 3000 }, (req) => handler(req, { env, dataDir: DATA }));
"""

ROUTER_DENO = r"""
const handlers = new Map();
const envs = new Map();
const dataDirs = new Map();
const DATA_ROOT = "__DATA_ROOT__";

async function ensureDataDir(name) {
  if (!DATA_ROOT) return "";
  const dir = `${DATA_ROOT}/${name}`;
  try { await Deno.mkdir(dir, { recursive: true }); } catch (e) {
    if (!(e instanceof Deno.errors.AlreadyExists)) console.error(`mkdir ${dir}:`, e.message);
  }
  return dir;
}

for await (const entry of Deno.readDir("/projects")) {
  if (!entry.isDirectory || entry.name.startsWith("_")) continue;
  try {
    const raw = await Deno.readTextFile(`/projects/${entry.name}/project.json`);
    const manifest = JSON.parse(raw);
    if (manifest.runtime !== "deno" && manifest.runtime !== "bun") continue;
    const entryFile = manifest.entry || "server.ts";
    const mod = await import(`file:///projects/${entry.name}/files/${entryFile}?t=${Date.now()}`);
    const handler = mod.default;
    if (typeof handler === "function") {
      handlers.set(entry.name, handler);
      envs.set(entry.name, manifest.env || {});
      dataDirs.set(entry.name, await ensureDataDir(entry.name));
      console.log(`Loaded: ${entry.name}`);
    }
  } catch (e) {
    console.error(`Failed to load ${entry.name}: ${e.message}`);
  }
}

console.log(`Router ready: ${handlers.size} projects`);

// Warmup: fire a synthetic request to each handler so they can initialize
// (e.g. inject env vars into background workers like setInterval loops)
for (const [name, handler] of handlers) {
  try {
    const warmupReq = new Request("http://localhost/_warmup");
    await handler(warmupReq, {env: envs.get(name) || {}, dataDir: dataDirs.get(name) || ""});
  } catch (e) {
    // Warmup errors are non-fatal (handler may not support GET /_warmup)
  }
}
console.log("Warmup complete");

  Deno.serve({ port: 3000 }, async (req) => {
  const url = new URL(req.url);
  const parts = url.pathname.split("/").filter(Boolean);
  const name = parts.shift() || "";
  const handler = handlers.get(name);
  if (!handler) {
    return new Response(JSON.stringify({projects: [...handlers.keys()]}),
      {status: 404, headers: {"content-type": "application/json"}});
  }
  const subpath = "/" + parts.join("/") + (url.search || "");
  const newReq = new Request(new URL(subpath, url.origin).toString(),
    {method: req.method, headers: req.headers, body: req.body});
  return handler(newReq, {env: envs.get(name) || {}, dataDir: dataDirs.get(name) || ""});
});
"""

ROUTER_NODE = r"""
const http = require("http");
const fs = require("fs");
const path = require("path");
const handlers = {};
const envs = {};

for (const name of fs.readdirSync("/projects")) {
  if (name.startsWith("_")) continue;
  const mp = `/projects/${name}/project.json`;
  if (!fs.existsSync(mp)) continue;
  const manifest = JSON.parse(fs.readFileSync(mp, "utf8"));
  if (manifest.runtime !== "node") continue;
  const entry = manifest.entry || "index.js";
  const modPath = `/projects/${name}/files/${entry}`;
  if (!fs.existsSync(modPath)) continue;
  try {
    handlers[name] = require(modPath);
    envs[name] = manifest.env || {};
    console.log(`Loaded: ${name}`);
  } catch(e) { console.error(`Failed to load ${name}: ${e.message}`); }
}

console.log(`Router ready: ${Object.keys(handlers).length} projects`);

// Warmup: fire a synthetic request to each handler so they can initialize
for (const name of Object.keys(handlers)) {
  try {
    const fakeReq = Object.create(http.IncomingMessage.prototype);
    fakeReq.method = "GET";
    fakeReq.url = "/_warmup";
    fakeReq.headers = {};
    let responded = false;
    const fakeRes = {
      statusCode: 200,
      setHeader: () => {},
      end: (data) => { responded = true; },
    };
    handlers[name](fakeReq, fakeRes, envs[name] || {});
  } catch (e) {
    // Warmup errors are non-fatal
  }
}
console.log("Warmup complete");

http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  const parts = url.pathname.split("/").filter(Boolean);
  const name = parts.shift() || "";
  const handler = handlers[name];
  if (!handler) {
    res.writeHead(404, {"Content-Type": "application/json"});
    res.end(JSON.stringify({projects: Object.keys(handlers)}));
    return;
  }
  req.url = "/" + parts.join("/") + (url.search || "");
  handler(req, res, envs[name] || {});
}).listen(3000, () => console.log("Node router listening on :3000"));
"""

ROUTER_PYTHON = r"""
import importlib.util, json, os, sys, asyncio
from aiohttp import web

handlers = {}
envs = {}

for name in sorted(os.listdir("/projects")):
    if name.startswith("_"): continue
    mp = f"/projects/{name}/project.json"
    if not os.path.isfile(mp): continue
    manifest = json.loads(open(mp).read())
    if manifest.get("runtime") != "python": continue
    entry = manifest.get("entry", "app.py")
    mod_path = f"/projects/{name}/files/{entry}"
    if not os.path.isfile(mod_path): continue
    try:
        spec = importlib.util.spec_from_file_location(f"proj_{name}", mod_path)
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, f"/projects/{name}/files")
        spec.loader.exec_module(mod)
        if hasattr(mod, "handle"):
            handlers[name] = mod.handle
            envs[name] = manifest.get("env", {})
            print(f"Loaded: {name}")
    except Exception as e:
        print(f"Failed to load {name}: {e}")

print(f"Router ready: {len(handlers)} projects")

# Warmup: fire a synthetic request to each handler so they can initialize
for name, handler in handlers.items():
    try:
        asyncio.get_event_loop().run_until_complete(
            handler("GET", "/_warmup", {}, b"", envs.get(name, {})))
    except Exception:
        pass
print("Warmup complete")

async def route(request):
    path = request.path.strip("/")
    parts = path.split("/", 1)
    name = parts[0]
    handler = handlers.get(name)
    if not handler:
        return web.json_response({"projects": list(handlers.keys())}, status=404)
    subpath = "/" + parts[1] if len(parts) > 1 else "/"
    body = await request.read()
    status, resp_headers, resp_body = await handler(
        request.method, subpath, dict(request.headers), body, envs.get(name, {}))
    return web.Response(body=resp_body, status=status, headers=resp_headers)

app = web.Application()
app.router.add_route("*", "/{path:.*}", route)
web.run_app(app, port=8000, print=lambda *a: None)
"""

RUNTIME_CONFIG = {
    "deno": {
        "image": "denoland/deno:latest",
        "cmd": ["deno", "run", "--allow-all", "--no-check", "/projects/_router.ts"],
        "port": 3000,
        "router_file": "_router.ts",
        "router_code": ROUTER_DENO,
    },
    "bun": {
        "image": "denoland/deno:latest",
        "cmd": ["deno", "run", "--allow-all", "--no-check", "/projects/_router.ts"],
        "port": 3000,
        "router_file": "_router.ts",
        "router_code": ROUTER_DENO,
    },
    "node": {
        "image": "node:22-slim",
        "cmd": ["node", "/projects/_router.js"],
        "port": 3000,
        "router_file": "_router.js",
        "router_code": ROUTER_NODE,
    },
    "python": {
        "image": "python:3.12-slim",
        "cmd": ["sh", "-c", "pip install -q aiohttp && python /projects/_router.py"],
        "port": 8000,
        "router_file": "_router.py",
        "router_code": ROUTER_PYTHON,
    },
}


class RuntimeManager:
    def __init__(self, docker: DockerClient, store: ProjectStore, tracker: ContainerTracker):
        self.docker = docker
        self.store = store
        self.tracker = tracker
        self.runtime_ips: dict[tuple[str, str], str] = {}  # (runtime_key, mode) -> ip
        self.runtime_cids: dict[tuple[str, str], str] = {}  # (runtime_key, mode) -> cid
        self.image_routes: dict[str, tuple[str, int]] = {}  # project name -> (ip, image_port)
        self.image_cids: dict[str, str] = {}  # project name -> cid

    async def refresh(self, runtime: str):
        if runtime == "static" or runtime == "dockerfile":
            return
        # bun shares deno's router
        config_key = runtime
        if config_key not in RUNTIME_CONFIG:
            return
        config = RUNTIME_CONFIG[config_key]

        # Check if any projects use this runtime in either mode
        runtimes_served = [config_key]
        if config_key == "deno":
            runtimes_served.append("bun")
        projects = [p for p in self.store.list()
                    if p.runtime in runtimes_served and p.isolation != "container"]

        # Handle dev and attested modes separately
        for mode in ["dev", "attested"]:
            mode_projects = [p for p in projects if p.mode == mode]
            mode_suffix = mode
            network = NETWORK_ATTESTED if mode == "attested" else NETWORK_DEV
            cname = f"tee-runtime-{config_key}-{mode_suffix}"
            key = (config_key, mode)
            # Split router filename for mode suffix insertion (before ext)
            rfile = config["router_file"]
            base_name, dot, ext = rfile.rpartition(".")
            if not dot:
                base_name, ext = rfile, ""

            router_path = os.path.join(self.store.base_dir, f"{base_name}.{mode_suffix}.{ext}")

            # Stop existing
            existing = await self.docker.container_exists(cname)
            if existing:
                await self.docker.stop(existing)
                await self.docker.remove(existing)
                self.tracker.remove(existing)

            if not mode_projects:
                self.runtime_ips.pop(key, None)
                self.runtime_cids.pop(key, None)
                log.info("No %s %s projects, skipping runtime container", mode_suffix, config_key)
                continue

            log.info("Pulling %s...", config["image"])
            await self.docker.pull(config["image"])

            labels = {"tee-proxy.managed": "true", "tee-daemon.runtime": config_key}
            if mode == "attested":
                labels["tee-daemon.attested"] = "true"

            # Add project labels for audit log association
            for p in mode_projects:
                labels[f"tee-daemon.project.{p.name}"] = "true"

            if VOLUME_NAME:
                # Docker-in-Docker: mount the named volume, projects are at subdir
                rel = os.path.relpath(self.store.base_dir, VOLUME_MOUNT)
                binds = [f"{VOLUME_NAME}:/daemon-vol:ro"]
                # Rewrite router to read from /daemon-vol/{rel}/
                projects_root = f"/daemon-vol/{rel}"
            else:
                # Local dev: host path works directly
                binds = [f"{os.path.abspath(self.store.base_dir)}:/projects:ro"]
                projects_root = "/projects"

            # Optional writable per-project data volume (same shape for both modes).
            data_root = ""
            if DATA_VOLUME_NAME:
                binds.append(f"{DATA_VOLUME_NAME}:{DATA_VOLUME_MOUNT_IN_RUNTIME}:rw")
                data_root = DATA_VOLUME_MOUNT_IN_RUNTIME

            # Write router with correct projects root, filtering by mode
            router_code = config["router_code"].replace("/projects/", f"{projects_root}/").replace('"/projects"', f'"{projects_root}"')
            router_code = router_code.replace("__DATA_ROOT__", data_root)
            # Filter projects by mode in the router
            if mode == "attested":
                router_code = router_code.replace(
                    'if (manifest.runtime !== "deno" && manifest.runtime !== "bun") continue;',
                    'if (manifest.mode !== "attested") continue; if (manifest.runtime !== "deno" && manifest.runtime !== "bun") continue;'
                )
            else:
                router_code = router_code.replace(
                    'if (manifest.runtime !== "deno" && manifest.runtime !== "bun") continue;',
                    'if (manifest.mode === "attested") continue; if (manifest.runtime !== "deno" && manifest.runtime !== "bun") continue;'
                )
            with open(router_path, "w") as f:
                f.write(router_code)

            cmd = [c.replace("/projects/", f"{projects_root}/") for c in config["cmd"]]
            # Insert mode suffix before file extension (e.g. _router.ts -> _router.dev.ts)
            cmd = [
                c.replace(
                    f".{ext}",
                    f".{mode_suffix}.{ext}"
                ) if config["router_file"].split("/")[-1] in c else c
                for c in cmd
            ]

            cid = await self.docker.create_container(
                cname, config["image"], cmd, binds, labels, network,
                runtime=CONTAINER_RUNTIME)
            await self.docker.start(cid)
            self.tracker.add(cid)
            ip = await self.docker.container_ip(cid, network)
            self.runtime_cids[key] = cid
            self.runtime_ips[key] = ip
            log.info("Runtime %s-%s -> %s (%s), serving %d projects",
                     config_key, mode_suffix, cid[:12], ip, len(mode_projects))

    async def _ensure_project_network(self, project_name: str, mode: str) -> str:
        net_name = f"tee-proj-{project_name}-{mode}"
        await self.docker.create_network(net_name)
        daemon_hostname = os.environ.get("HOSTNAME", "")
        if daemon_hostname:
            try:
                await self.docker.connect_network(daemon_hostname, net_name)
            except Exception as e:
                log.debug("daemon connect_network %s: %s", net_name, e)
        return net_name

    async def start_isolated(self, project) -> str:
        """Per-project container for deno/bun with scoped Deno permissions.
        Returns the runtime image digest."""
        if project.runtime not in ("deno", "bun"):
            raise ValueError(f"isolation=container not implemented for runtime {project.runtime}")
        config = RUNTIME_CONFIG["deno"]
        image = config["image"]
        await self.docker.pull(image)

        files_dir = self.store.files_dir(project.name)
        entry_path = os.path.join(self.store._project_dir(project.name), "_entry.ts")
        with open(entry_path, "w") as f:
            f.write(_ENTRY_SHIM_DENO)

        cname = f"tee-isolated-{project.name}-{project.mode}"
        network = await self._ensure_project_network(project.name, project.mode)
        existing = await self.docker.container_exists(cname)
        if existing:
            await self.docker.stop(existing)
            await self.docker.remove(existing)
            self.tracker.remove(existing)

        if VOLUME_NAME:
            rel_files = os.path.relpath(files_dir, VOLUME_MOUNT)
            rel_entry = os.path.relpath(entry_path, VOLUME_MOUNT)
            binds = [f"{VOLUME_NAME}:/daemon-vol:ro"]
            files_in = f"/daemon-vol/{rel_files}"
            entry_in = f"/daemon-vol/{rel_entry}"
        else:
            binds = [
                f"{os.path.abspath(files_dir)}:/files:ro",
                f"{os.path.abspath(entry_path)}:/_entry.ts:ro",
            ]
            files_in = "/files"
            entry_in = "/_entry.ts"

        # Per-project data volume — only this project's data is visible to it.
        proj_data_volume = f"tee-projdata-{project.name}"
        await self.docker.ensure_volume(proj_data_volume)
        binds.append(f"{proj_data_volume}:/data:rw")
        data_dir_in = "/data"

        labels = {
            "tee-proxy.managed": "true",
            "tee-daemon.runtime": project.runtime,
            "tee-daemon.isolation": "container",
            f"tee-daemon.project.{project.name}": "true",
        }
        if project.mode == "attested":
            labels["tee-daemon.attested"] = "true"

        cmd = [
            "deno", "run", "--no-prompt",
            f"--allow-read={files_in},{entry_in}" + (f",{data_dir_in}" if data_dir_in else ""),
            "--allow-net",
            "--deny-env", "--deny-ffi", "--deny-run", "--deny-sys",
        ]
        if data_dir_in:
            cmd.append(f"--allow-write={data_dir_in}")
        cmd += [
            entry_in,
            project.entry or "server.ts",
            files_in,
            data_dir_in,
            json.dumps(project.env or {}),
        ]

        cid = await self.docker.create_container(
            cname, image, cmd, binds, labels, network,
            runtime=CONTAINER_RUNTIME)
        await self.docker.start(cid)
        self.tracker.add(cid)
        ip = await self.docker.container_ip(cid, network)
        self.image_cids[project.name] = cid
        self.image_routes[project.name] = (ip, 3000)
        log.info("Isolated %s project %s -> %s (%s:3000)",
                 project.runtime, project.name, cid[:12], ip)
        return await self.docker.image_digest(image)

    async def stop_isolated(self, name: str):
        for mode in ("dev", "attested"):
            cname = f"tee-isolated-{name}-{mode}"
            existing = await self.docker.container_exists(cname)
            if existing:
                await self.docker.stop(existing)
                await self.docker.remove(existing)
                self.tracker.remove(existing)
        self.image_routes.pop(name, None)
        self.image_cids.pop(name, None)

    async def start_image(self, project) -> str:
        """Pull and start an image-runtime project's container. Returns image digest."""
        cname = f"tee-image-{project.name}-{project.mode}"
        network = await self._ensure_project_network(project.name, project.mode)
        log.info("Pulling %s for project %s...", project.image, project.name)
        await self.docker.pull(project.image)
        existing = await self.docker.container_exists(cname)
        if existing:
            await self.docker.stop(existing)
            await self.docker.remove(existing)
            self.tracker.remove(existing)
        binds = []
        for v in project.volumes or []:
            await self.docker.ensure_volume(v["name"])
            binds.append(f"{v['name']}:{v['mount']}")
        labels = {
            "tee-proxy.managed": "true",
            "tee-daemon.runtime": "image",
            f"tee-daemon.project.{project.name}": "true",
        }
        if project.mode == "attested":
            labels["tee-daemon.attested"] = "true"
        env = [f"{k}={v}" for k, v in (project.env or {}).items()]
        for key in project.env_passthrough or []:
            val = os.environ.get(key)
            if val is not None:
                env.append(f"{key}={val}")
        cid = await self.docker.create_container(
            cname, project.image, [], binds, labels, network,
            env=env, runtime=CONTAINER_RUNTIME)
        await self.docker.start(cid)
        self.tracker.add(cid)
        ip = await self.docker.container_ip(cid, network)
        self.image_cids[project.name] = cid
        self.image_routes[project.name] = (ip, project.image_port)
        log.info("Image project %s -> %s (%s:%d)", project.name, cid[:12], ip, project.image_port)
        return await self.docker.image_digest(project.image)

    async def stop_image(self, name: str):
        for mode in ("dev", "attested"):
            cname = f"tee-image-{name}-{mode}"
            existing = await self.docker.container_exists(cname)
            if existing:
                await self.docker.stop(existing)
                await self.docker.remove(existing)
                self.tracker.remove(existing)
        self.image_routes.pop(name, None)
        self.image_cids.pop(name, None)

    def get_image_route(self, name: str) -> tuple[str, int] | None:
        return self.image_routes.get(name)

    def get_route(self, runtime: str, mode: str) -> tuple[str, int] | None:
        if runtime == "static" or runtime == "dockerfile" or runtime == "image":
            return None
        config_key = runtime
        if runtime == "bun":
            config_key = "deno"
        if mode not in ("dev", "attested"):
            mode = "dev"
        key = (config_key, mode)
        ip = self.runtime_ips.get(key)
        if not ip:
            return None
        return (ip, RUNTIME_CONFIG[config_key]["port"])

    async def recover_all(self):
        runtimes_needed = set()
        image_projects = []
        isolated_projects = []
        for p in self.store.list():
            if p.runtime == "image":
                image_projects.append(p)
            elif p.isolation == "container" and p.runtime in ("deno", "bun"):
                isolated_projects.append(p)
            elif p.runtime not in ("static", "dockerfile"):
                runtimes_needed.add(p.runtime)
        for rt in runtimes_needed:
            await self.refresh(rt)
        for p in image_projects:
            await self.start_image(p)
        for p in isolated_projects:
            await self.start_isolated(p)

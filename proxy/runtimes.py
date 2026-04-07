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

ROUTER_DENO = r"""
const handlers = new Map<string, (req: Request, ctx: {env: Record<string,string>}) => Response | Promise<Response>>();
const envs = new Map<string, Record<string,string>>();

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
    await handler(warmupReq, {env: envs.get(name) || {}});
  } catch (e) {
    // Warmup errors are non-fatal (handler may not support GET /_warmup)
  }
}
console.log("Warmup complete");

Deno.serve({ port: 3000 }, async (req: Request) => {
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
  return handler(newReq, {env: envs.get(name) || {}});
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
        "cmd": ["deno", "run", "--allow-all", "/projects/_router.ts"],
        "port": 3000,
        "router_file": "_router.ts",
        "router_code": ROUTER_DENO,
    },
    "bun": {
        "image": "denoland/deno:latest",
        "cmd": ["deno", "run", "--allow-all", "/projects/_router.ts"],
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
        projects = [p for p in self.store.list() if p.runtime in runtimes_served]

        # Handle dev and attested modes separately
        for mode in ["dev", "attested"]:
            mode_projects = [p for p in projects if p.mode == mode]
            mode_suffix = mode
            network = NETWORK_ATTESTED if mode == "attested" else NETWORK_DEV
            cname = f"tee-runtime-{config_key}-{mode_suffix}"
            key = (config_key, mode)

            router_path = os.path.join(self.store.base_dir, f"{config['router_file']}.{mode_suffix}")

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

            # Write router with correct projects root, filtering by mode
            router_code = config["router_code"].replace("/projects/", f"{projects_root}/").replace('"/projects"', f'"{projects_root}"')
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
            # Append mode suffix to router file (e.g. _router.ts -> _router.ts.dev)
            cmd = [
                c.replace(
                    f".{config['router_file'].split('.')[-1]}",
                    f".{config['router_file'].split('.')[-1]}.{mode_suffix}"
                ) if config["router_file"].split("/")[-1] in c else c
                for c in cmd
            ]

            cid = await self.docker.create_container(
                cname, config["image"], cmd, binds, labels, network)
            await self.docker.start(cid)
            self.tracker.add(cid)
            ip = await self.docker.container_ip(cid, network)
            self.runtime_cids[key] = cid
            self.runtime_ips[key] = ip
            log.info("Runtime %s-%s -> %s (%s), serving %d projects",
                     config_key, mode_suffix, cid[:12], ip, len(mode_projects))

    def get_route(self, runtime: str, mode: str) -> tuple[str, int] | None:
        if runtime == "static" or runtime == "dockerfile":
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
        for p in self.store.list():
            if p.runtime not in ("static", "dockerfile"):
                runtimes_needed.add(p.runtime)
        for rt in runtimes_needed:
            await self.refresh(rt)

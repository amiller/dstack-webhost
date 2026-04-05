"""Ingress reverse proxy + management API on port 8080."""

import hmac
import json
import logging
import os
from dataclasses import asdict

import aiohttp
from aiohttp import web

from .docker_client import DockerClient
from .projects import ProjectStore
from .tracker import ContainerTracker
from .audit import AuditLog
from .deploy import deploy, teardown
from .runtimes import RuntimeManager

log = logging.getLogger(__name__)

DSTACK_SOCK = None  # set by main.py
API_TOKEN = os.environ.get("TEE_DAEMON_TOKEN", "")

MIME_TYPES = {
    ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
    ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg",
    ".svg": "image/svg+xml", ".ico": "image/x-icon", ".txt": "text/plain",
    ".woff2": "font/woff2", ".woff": "font/woff",
}


class Ingress:
    def __init__(self, store: ProjectStore, docker: DockerClient,
                 audit: AuditLog, tracker: ContainerTracker, rtm: RuntimeManager):
        self.store = store
        self.docker = docker
        self.audit = audit
        self.tracker = tracker
        self.rtm = rtm

    async def handle(self, request: web.Request) -> web.Response:
        path = request.path.lstrip("/")

        if path.startswith("_api"):
            return await self._handle_api(request, path[4:].lstrip("/"))

        parts = path.split("/", 1)
        name = parts[0] if parts[0] else ""

        if not name:
            projects = {p.name: {"runtime": p.runtime, "attested": p.attested}
                        for p in self.store.list()}
            return web.json_response({"projects": projects})

        try:
            project = self.store.load(name)
        except FileNotFoundError:
            return web.json_response({"error": "not found"}, status=404)

        subpath = "/" + parts[1] if len(parts) > 1 else "/"

        if project.runtime == "static":
            return self._serve_static(project, subpath)

        route = self.rtm.get_route(project.runtime)
        if not route:
            return web.json_response({"error": "runtime not running"}, status=503)

        ip, port = route
        # Prefix project name back for the shared router
        routed_path = f"/{name}{subpath}"
        qs = request.query_string
        if qs:
            routed_path += "?" + qs
        return await self._proxy(request, ip, port, routed_path)

    def _serve_static(self, project, subpath: str) -> web.Response:
        files_dir = self.store.files_dir(project.name)
        entry = project.entry if project.entry != "." else ""
        base = os.path.join(files_dir, entry)
        requested = os.path.normpath(os.path.join(base, subpath.lstrip("/")))

        if not requested.startswith(os.path.normpath(base)):
            return web.Response(status=403)
        if "/.git" in requested or requested.endswith(".git"):
            return web.Response(status=403)

        if os.path.isdir(requested):
            requested = os.path.join(requested, "index.html")
        if not os.path.isfile(requested):
            return web.Response(status=404)

        ext = os.path.splitext(requested)[1].lower()
        ct = MIME_TYPES.get(ext, "application/octet-stream")
        with open(requested, "rb") as f:
            return web.Response(body=f.read(), content_type=ct)

    async def _proxy(self, request: web.Request, ip: str, port: int,
                     path: str) -> web.Response:
        body = await request.read()
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "transfer-encoding", "accept-encoding")}
        url = f"http://{ip}:{port}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.request(request.method, url,
                                       data=body if body else None,
                                       headers=headers) as resp:
                resp_body = await resp.read()
                return web.Response(
                    body=resp_body, status=resp.status,
                    content_type=resp.content_type)

    def _check_auth(self, request: web.Request) -> web.Response | None:
        if not API_TOKEN:
            return None
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return web.json_response({"error": "missing token"}, status=401)
        token = auth[7:]
        if not hmac.compare_digest(token, API_TOKEN):
            return web.json_response({"error": "invalid token"}, status=403)
        return None

    async def _handle_api(self, request: web.Request, path: str) -> web.Response:
        denied = self._check_auth(request)
        if denied:
            return denied
        method = request.method

        if path == "projects" and method == "GET":
            return await self._api_list()

        if path == "projects" and method == "POST":
            return await self._api_deploy(request)

        if path.startswith("projects/"):
            name = path.split("/")[1]
            rest = "/".join(path.split("/")[2:])

            if method == "GET" and not rest:
                return await self._api_status(name)
            if method == "DELETE" and not rest:
                return await self._api_teardown(name)
            if method == "POST" and rest == "redeploy":
                return await self._api_redeploy(name)

        if path.startswith("attest/"):
            name = path.split("/")[1]
            return await self._api_attest(name)

        if path == "audit" and method == "GET":
            return web.json_response(self.audit.to_json())

        return web.json_response({"error": "not found"}, status=404)

    async def _api_list(self) -> web.Response:
        projects = self.store.list()
        return web.json_response([asdict(p) for p in projects])

    async def _api_deploy(self, request: web.Request) -> web.Response:
        manifest = await request.json()
        project = await deploy(
            self.store, self.docker, self.audit, self.tracker, self.rtm, manifest)
        return web.json_response(asdict(project), status=201)

    async def _api_status(self, name: str) -> web.Response:
        project = self.store.load(name)
        return web.json_response(asdict(project))

    async def _api_teardown(self, name: str) -> web.Response:
        await teardown(self.store, self.docker, self.audit, self.tracker,
                       self.rtm, name)
        return web.json_response({"ok": True})

    async def _api_redeploy(self, name: str) -> web.Response:
        project = self.store.load(name)
        old_sha = project.commit_sha
        manifest = {
            "name": project.name, "source": project.source, "ref": project.ref,
            "runtime": project.runtime, "entry": project.entry, "port": project.port,
            "attested": project.attested, "env": project.env,
            "source_path": project.source_path,
        }
        project = await deploy(
            self.store, self.docker, self.audit, self.tracker, self.rtm, manifest)
        result = asdict(project)
        result["changed"] = project.commit_sha != old_sha
        return web.json_response(result)

    async def _api_attest(self, name: str) -> web.Response:
        project = self.store.load(name)
        if not project.attested:
            return web.json_response({"error": "project not attested"}, status=400)
        if not DSTACK_SOCK:
            return web.json_response({"error": "dstack not available"}, status=503)

        key_path = f"/tee-daemon/projects/{name}"
        body = {"path": key_path}
        conn = aiohttp.UnixConnector(path=DSTACK_SOCK)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.post("http://localhost/GetKey", json=body) as resp:
                data = await resp.json()
                return web.json_response(data, status=resp.status)

"""Docker socket proxy — filters Docker Engine API requests and logs operations."""

import json
import re
import logging

import aiohttp
from aiohttp import web

from .tracker import ContainerTracker
from .audit import AuditLog, AuditEntry

log = logging.getLogger(__name__)

LABEL_MANAGED = "tee-proxy.managed"
NETWORK_NAME = "tee-apps"

OPEN_ROUTES = [
    ("GET",  re.compile(r"^(/v[\d.]+)?/_ping$")),
    ("HEAD", re.compile(r"^(/v[\d.]+)?/_ping$")),
    ("GET",  re.compile(r"^(/v[\d.]+)?/version$")),
    ("GET",  re.compile(r"^(/v[\d.]+)?/images/json$")),
    ("POST", re.compile(r"^(/v[\d.]+)?/images/create$")),
    ("GET",  re.compile(r"^(/v[\d.]+)?/info$")),
]

STREAM_OPEN_ROUTES = [
    ("GET",  re.compile(r"^(/v[\d.]+)?/events$")),
]

TRACKED_ROUTES = [
    ("POST",   re.compile(r"^(/v[\d.]+)?/containers/([a-f0-9]+)/start$"), "start"),
    ("POST",   re.compile(r"^(/v[\d.]+)?/containers/([a-f0-9]+)/stop$"), "stop"),
    ("POST",   re.compile(r"^(/v[\d.]+)?/containers/([a-f0-9]+)/kill$"), "kill"),
    ("DELETE", re.compile(r"^(/v[\d.]+)?/containers/([a-f0-9]+)$"), "remove"),
    ("GET",    re.compile(r"^(/v[\d.]+)?/containers/([a-f0-9]+)/json$"), "inspect"),
    ("GET",    re.compile(r"^(/v[\d.]+)?/containers/([a-f0-9]+)/logs$"), "logs"),
    ("POST",   re.compile(r"^(/v[\d.]+)?/containers/([a-f0-9]+)/attach$"), "attach"),
    ("POST",   re.compile(r"^(/v[\d.]+)?/containers/([a-f0-9]+)/wait$"), "wait"),
]

# Hard deny — exec and archive break container isolation boundary
# attach is allowed for tracked containers (needed by docker run)
DENIED_RE = re.compile(r"^(/v[\d.]+)?/containers/[a-f0-9]+/(exec|archive)$")

CREATE_RE = re.compile(r"^(/v[\d.]+)?/containers/create$")
LIST_RE = re.compile(r"^(/v[\d.]+)?/containers/json$")
AUDIT_RE = re.compile(r"^(/v[\d.]+)?/tee-proxy/audit$")


class DockerProxy:
    def __init__(self, real_socket: str, tracker: ContainerTracker, audit: AuditLog):
        self.real_socket = real_socket
        self.tracker = tracker
        self.audit = audit

    async def ensure_network(self):
        conn = aiohttp.UnixConnector(path=self.real_socket)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.get("http://localhost/networks") as resp:
                networks = await resp.json()
                if any(n["Name"] == NETWORK_NAME for n in networks):
                    return
            async with session.post("http://localhost/networks/create",
                                    json={"Name": NETWORK_NAME, "Driver": "bridge"}) as resp:
                log.info("Created network %s: %s", NETWORK_NAME, resp.status)

    async def recover_tracked(self):
        conn = aiohttp.UnixConnector(path=self.real_socket)
        async with aiohttp.ClientSession(connector=conn) as session:
            params = {"filters": json.dumps({"label": [f"{LABEL_MANAGED}=true"]})}
            async with session.get("http://localhost/containers/json", params=params) as resp:
                for c in await resp.json():
                    self.tracker.add(c["Id"])
                    log.info("Recovered tracked container %s", c["Id"][:12])

    async def handle(self, request: web.Request) -> web.Response:
        method = request.method
        path = request.path_qs

        if DENIED_RE.match(request.path):
            log.warning("Denied %s %s (isolation boundary)", method, request.path)
            return web.json_response({"message": "Operation not permitted"}, status=403)

        if AUDIT_RE.match(request.path):
            return web.json_response(self.audit.to_json())

        for m, pat in OPEN_ROUTES:
            if method == m and pat.match(request.path):
                return await self._forward(method, path, request)

        for m, pat in STREAM_OPEN_ROUTES:
            if method == m and pat.match(request.path):
                return await self._forward(method, path, request, stream=True)

        if method == "POST" and CREATE_RE.match(request.path):
            return await self._handle_create(path, request)

        if method == "GET" and LIST_RE.match(request.path):
            return await self._handle_list(path, request)

        for m, pat, action in TRACKED_ROUTES:
            match = pat.match(request.path)
            if method == m and match:
                cid = match.group(2)
                if not self.tracker.is_allowed(cid):
                    return web.json_response(
                        {"message": f"Container {cid[:12]} not managed by proxy"}, status=403)
                resp = await self._forward(method, path, request,
                                          stream=(action in ("logs", "attach")))
                if resp.status < 400 and action in ("start", "stop", "kill", "remove"):
                    import time
                    await self.audit.record(AuditEntry(
                        timestamp=time.time(), action=action,
                        container_id=self.tracker.full_id(cid) or cid))
                    if action == "remove":
                        self.tracker.remove(cid)
                return resp

        log.warning("Denied %s %s", method, request.path)
        return web.json_response({"message": "Operation not permitted"}, status=403)

    async def _handle_create(self, path: str, request: web.Request) -> web.Response:
        body = await request.json()
        image = body.get("Image", "")

        labels = body.setdefault("Labels", {})
        labels[LABEL_MANAGED] = "true"

        # Replace whatever network the client requested with tee-apps
        host_config = body.get("HostConfig", {})
        host_config.pop("NetworkMode", None)
        body["HostConfig"] = host_config

        body["NetworkingConfig"] = {"EndpointsConfig": {NETWORK_NAME: {}}}

        conn = aiohttp.UnixConnector(path=self.real_socket)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.post(f"http://localhost{path}", json=body) as resp:
                data = await resp.json()
                if resp.status < 400 and "Id" in data:
                    cid = data["Id"]
                    self.tracker.add(cid)

                    # Resolve image digest from inspect
                    image_digest = ""
                    try:
                        async with session.get(f"http://localhost/images/{image}/json") as img_resp:
                            if img_resp.status == 200:
                                img_data = await img_resp.json()
                                image_digest = img_data.get("Id", "")
                    except Exception:
                        pass

                    import time
                    await self.audit.record(AuditEntry(
                        timestamp=time.time(), action="create",
                        container_id=cid, image=image, image_digest=image_digest))

                return web.json_response(data, status=resp.status)

    async def _handle_list(self, path: str, request: web.Request) -> web.Response:
        conn = aiohttp.UnixConnector(path=self.real_socket)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.get(f"http://localhost{path}") as resp:
                containers = await resp.json()
                filtered = [c for c in containers if self.tracker.is_allowed(c["Id"])]
                return web.json_response(filtered)

    async def _forward(self, method: str, path: str, request: web.Request,
                       stream: bool = False) -> web.Response:
        body = await request.read()
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "transfer-encoding")}
        conn = aiohttp.UnixConnector(path=self.real_socket)
        session = aiohttp.ClientSession(connector=conn)
        resp = await session.request(method, f"http://localhost{path}",
                                     data=body if body else None, headers=headers)
        if stream:
            sr = web.StreamResponse(status=resp.status,
                                    headers={"Content-Type": resp.content_type or "application/octet-stream"})
            await sr.prepare(request)
            async for chunk in resp.content.iter_any():
                await sr.write(chunk)
            await sr.write_eof()
            resp.close()
            await session.close()
            return sr
        resp_body = await resp.read()
        resp.close()
        await session.close()
        return web.Response(body=resp_body, status=resp.status,
                            content_type=resp.content_type)

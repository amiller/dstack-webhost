"""Ingress reverse proxy + management API on port 8080."""

import asyncio
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
from .audit import AuditLogManager
from .deploy import deploy, teardown, promote
from .runtimes import RuntimeManager
from .tunnel import TunnelStore, TunnelResponse

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
                 audit_manager: AuditLogManager, tracker: ContainerTracker,
                 rtm: RuntimeManager, tunnel_store: TunnelStore):
        self.store = store
        self.docker = docker
        self.audit_manager = audit_manager
        self.tracker = tracker
        self.rtm = rtm
        self.tunnel_store = tunnel_store
        self.port_map: dict[int, str] = {}  # port -> project_name

    async def handle(self, request: web.Request) -> web.Response:
        path = request.path.lstrip("/")

        if path.startswith("_api"):
            return await self._handle_api(request, path[4:].lstrip("/"))

        # Handle tunnel requests: /t/<tunnel-id>/...
        if path.startswith("t/"):
            return await self._handle_tunnel(request, path[2:].lstrip("/"))

        # Check if this request is on a custom port (port-based routing)
        local_port = request.transport.get_extra_info("sockname")[1]
        if local_port in self.port_map:
            project_name = self.port_map[local_port]
            return await self._handle_port_based(request, project_name, path)

        # Default: path-based routing on port 8080
        parts = path.split("/", 1)
        name = parts[0] if parts[0] else ""

        if not name:
            projects = {p.name: {"runtime": p.runtime, "mode": p.mode}
                        for p in self.store.list()}
            return web.json_response({"projects": projects})

        try:
            project = self.store.load(name)
        except FileNotFoundError:
            return web.json_response({"error": "not found"}, status=404)

        subpath = "/" + parts[1] if len(parts) > 1 else "/"

        # Serve verification page at /.well-known/tee-attestation/{name}
        if subpath == "/.well-known/tee-attestation":
            return self._serve_verification(name)

        if project.runtime == "static":
            return self._serve_static(project, subpath)

        route = self.rtm.get_route(project.runtime, project.mode)
        if not route:
            return web.json_response({"error": "runtime not running"}, status=503)

        ip, port = route
        # Prefix project name back for the shared router
        routed_path = f"/{name}{subpath}"
        qs = request.query_string
        if qs:
            routed_path += "?" + qs
        return await self._proxy(request, ip, port, routed_path)

    async def _handle_port_based(self, request: web.Request, project_name: str, path: str) -> web.Response:
        """Handle requests on custom ports - route directly to the project."""
        try:
            project = self.store.load(project_name)
        except FileNotFoundError:
            return web.json_response({"error": "project not found"}, status=404)

        subpath = "/" + path if path else "/"

        # Serve verification page at /.well-known/tee-attestation
        if subpath == "/.well-known/tee-attestation":
            return self._serve_verification(project_name)

        if project.runtime == "static":
            return self._serve_static(project, subpath)

        route = self.rtm.get_route(project.runtime, project.mode)
        if not route:
            return web.json_response({"error": "runtime not running"}, status=503)

        ip, port = route
        # For port-based routing, pass the path directly without prefixing
        qs = request.query_string
        if qs:
            subpath += "?" + qs
        return await self._proxy(request, ip, port, subpath)

    def _serve_verification(self, project_name: str) -> web.Response:
        """Serve the verification page for an attested project."""
        try:
            project = self.store.load(project_name)
            if project.mode != "attested":
                return web.json_response({"error": "project not attested"}, status=400)

            template_dir = os.path.join(os.path.dirname(__file__), "templates")
            template_path = os.path.join(template_dir, "verification.html")

            with open(template_path, "r") as f:
                template = f.read()

            # Replace template variables
            html = template.replace("{{ project_name }}", project_name)

            return web.Response(text=html, content_type="text/html")
        except FileNotFoundError:
            return web.json_response({"error": "project not found"}, status=404)
        except Exception as e:
            log.error("Failed to serve verification page: %s", e)
            return web.json_response({"error": "internal server error"}, status=500)

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

    def update_port_map(self):
        """Update the port map based on current projects.

        Port 8080 is reserved for path-based routing on the default ingress
        and is never registered for port-based routing, even if a project's
        listen config requests it. Such a project is reachable at /<name>/
        on port 8080 via path-based routing.
        """
        self.port_map.clear()
        for project in self.store.list():
            if project.listen and project.listen.port:
                port = project.listen.port
                if port == 8080:
                    continue
                if port not in self.port_map:
                    self.port_map[port] = project.name
                else:
                    # Port conflict - skip this project (logged elsewhere)
                    log.warning("Port conflict: %s wants port %d already used by %s",
                               project.name, port, self.port_map[port])

    def get_tcp_projects(self) -> list[tuple[int, str]]:
        """Get list of (port, project_name) for projects with TCP protocol."""
        tcp_projects = []
        for project in self.store.list():
            if project.listen and project.listen.protocol == "tcp":
                tcp_projects.append((project.listen.port, project.name))
        return tcp_projects

    async def create_tcp_server(self, port: int, project_name: str) -> asyncio.Server:
        """Create a TCP server that proxies raw connections to the backend."""
        async def handle_client(client_reader: asyncio.StreamReader,
                               client_writer: asyncio.StreamWriter):
            """Handle a TCP client connection."""
            try:
                project = self.store.load(project_name)
                route = self.rtm.get_route(project.runtime, project.mode)
                if not route:
                    log.error("Runtime not running for TCP project %s", project_name)
                    client_writer.close()
                    await client_writer.wait_closed()
                    return

                backend_ip, backend_port = route

                # Connect to backend
                try:
                    backend_reader, backend_writer = await asyncio.wait_for(
                        asyncio.open_connection(backend_ip, backend_port),
                        timeout=5.0
                    )
                except Exception as e:
                    log.error("Failed to connect to backend %s:%d for TCP project %s: %s",
                             backend_ip, backend_port, project_name, e)
                    client_writer.close()
                    await client_writer.wait_closed()
                    return

                # Bidirectional byte forwarding
                async def forward_client_to_backend():
                    try:
                        while True:
                            data = await client_reader.read(4096)
                            if not data:
                                break
                            backend_writer.write(data)
                            await backend_writer.drain()
                    except Exception:
                        pass
                    finally:
                        try:
                            backend_writer.close()
                            await backend_writer.wait_closed()
                        except Exception:
                            pass

                async def forward_backend_to_client():
                    try:
                        while True:
                            data = await backend_reader.read(4096)
                            if not data:
                                break
                            client_writer.write(data)
                            await client_writer.drain()
                    except Exception:
                        pass
                    finally:
                        try:
                            client_writer.close()
                            await client_writer.wait_closed()
                        except Exception:
                            pass

                # Run both forwarding tasks
                await asyncio.gather(
                    forward_client_to_backend(),
                    forward_backend_to_client(),
                    return_exceptions=True
                )

            except Exception as e:
                log.error("Error in TCP connection for project %s: %s", project_name, e)
            finally:
                try:
                    client_writer.close()
                    await client_writer.wait_closed()
                except Exception:
                    pass

        return await asyncio.start_server(handle_client, "0.0.0.0", port)

    async def _handle_tunnel(self, request: web.Request, path: str) -> web.Response:
        """Handle tunnel proxy requests: /t/<tunnel-id>/..."""
        parts = path.split("/", 1)
        tunnel_id = parts[0] if parts[0] else ""

        if not tunnel_id:
            return web.json_response({"error": "tunnel id required"}, status=400)

        # Get tunnel by ID
        tunnel = self.tunnel_store.get(tunnel_id)
        if not tunnel:
            return web.json_response({"error": "tunnel not found or expired"}, status=404)

        # Reconstruct the path to proxy to backend
        subpath = "/" + parts[1] if len(parts) > 1 else "/"
        qs = request.query_string
        if qs:
            subpath += "?" + qs

        # Handle WebSocket upgrade
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._proxy_websocket(request, tunnel.backend, subpath)

        # Handle regular HTTP request - make request to backend URL
        body = await request.read()
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "transfer-encoding", "accept-encoding")}
        url = f"{tunnel.backend}{subpath}"
        async with aiohttp.ClientSession() as session:
            async with session.request(request.method, url,
                                       data=body if body else None,
                                       headers=headers) as resp:
                resp_body = await resp.read()
                return web.Response(
                    body=resp_body, status=resp.status,
                    content_type=resp.content_type)

    async def _proxy_websocket(self, request: web.Request, backend_url: str, path: str) -> web.Response:
        """Proxy WebSocket connection to backend."""
        try:
            import aiohttp

            # Extract WebSocket headers
            ws_headers = {k: v for k, v in request.headers.items()
                          if k.lower() not in ("host", "connection", "upgrade", "transfer-encoding")}

            # Construct full backend URL with path
            full_url = f"{backend_url}{path}"

            # Create client WebSocket connection
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(full_url, headers=ws_headers) as client_ws:
                    # Create server-side WebSocket response
                    ws = web.WebSocketResponse()
                    await ws.prepare(request)

                    # Bidirectional byte forwarding
                    async def forward_client_to_server():
                        try:
                            async for msg in ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    await client_ws.send_str(msg.data)
                                elif msg.type == aiohttp.WSMsgType.BINARY:
                                    await client_ws.send_bytes(msg.data)
                                elif msg.type == aiohttp.WSMsgType.CLOSE:
                                    await client_ws.close(code=msg.data)
                                    break
                        except Exception as e:
                            log.warning("Error forwarding client to server WS: %s", e)
                        finally:
                            try:
                                await client_ws.close()
                            except Exception:
                                pass

                    async def forward_server_to_client():
                        try:
                            async for msg in client_ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    await ws.send_str(msg.data)
                                elif msg.type == aiohttp.WSMsgType.BINARY:
                                    await ws.send_bytes(msg.data)
                                elif msg.type == aiohttp.WSMsgType.CLOSE:
                                    await ws.close(code=msg.data)
                                    break
                        except Exception as e:
                            log.warning("Error forwarding server to client WS: %s", e)
                        finally:
                            try:
                                await ws.close()
                            except Exception:
                                pass

                    # Run both forwarding tasks
                    await asyncio.gather(
                        forward_client_to_server(),
                        forward_server_to_client(),
                        return_exceptions=True
                    )

                    return ws
        except Exception as e:
            log.error("WebSocket proxy error: %s", e)
            return web.json_response({"error": "websocket proxy failed"}, status=500)

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

    def _public_attested_path(self, path: str) -> str | None:
        """RFC 0015: return project name if `path` is a public verifier endpoint."""
        parts = path.split("/")
        if len(parts) == 2 and parts[0] in ("attest", "verification") and parts[1]:
            return parts[1]
        if len(parts) == 2 and parts[0] == "projects" and parts[1]:
            return parts[1]
        if len(parts) == 3 and parts[0] == "projects" and parts[1] and parts[2] == "audit":
            return parts[1]
        return None

    async def _handle_api(self, request: web.Request, path: str) -> web.Response:
        method = request.method

        # RFC 0015: read-only verifier endpoints are public for attested projects.
        # A relying party should not need the admin token to verify what is running.
        if method == "GET":
            public_name = self._public_attested_path(path)
            if public_name is not None:
                try:
                    project = self.store.load(public_name)
                except FileNotFoundError:
                    return web.json_response({"error": "not found"}, status=404)
                if project.mode != "attested":
                    return web.json_response({"error": "not found"}, status=404)
                if path.startswith("attest/"):
                    return await self._api_attest(public_name)
                if path.startswith("verification/"):
                    return await self._api_verification(public_name)
                if path.endswith("/audit"):
                    return await self._api_audit(public_name)
                return await self._api_status(public_name)

        denied = self._check_auth(request)
        if denied:
            return denied

        # Tunnel API endpoints
        if path == "tunnels" and method == "POST":
            return await self._api_create_tunnel(request)
        if path == "tunnels" and method == "GET":
            return await self._api_list_tunnels()
        if path.startswith("tunnels/") and method == "DELETE":
            tunnel_id = path.split("/")[1]
            return await self._api_delete_tunnel(tunnel_id)

        if path == "routes" and method == "GET":
            return await self._api_routes()

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
            if method == "POST" and rest == "promote":
                return await self._api_promote(name)
            if method == "GET" and rest == "audit":
                return await self._api_audit(name)

        if path.startswith("attest/"):
            name = path.split("/")[1]
            return await self._api_attest(name)

        if path.startswith("verification/"):
            name = path.split("/")[1]
            return await self._api_verification(name)

        return web.json_response({"error": "not found"}, status=404)

    async def _api_list(self) -> web.Response:
        projects = self.store.list()
        return web.json_response([asdict(p) for p in projects])

    async def _api_routes(self) -> web.Response:
        """Get the current routing table."""
        routes = []

        # Add default ingress port (8080) for path-based routing
        routes.append({
            "host_port": 8080,
            "protocol": "http",
            "project": "(ingress)",
            "backend": "path-based routing"
        })

        # Add custom port routes for projects
        for project in self.store.list():
            if project.listen and project.listen.port:
                port = project.listen.port
                protocol = project.listen.protocol or "http"

                if project.runtime == "static":
                    backend = "static files"
                elif project.runtime == "dockerfile":
                    backend = f"container:{project.container_id or 'unknown'}"
                else:
                    route = self.rtm.get_route(project.runtime, project.mode)
                    if route:
                        backend = f"{route[0]}:{route[1]}"
                    else:
                        backend = "runtime not running"

                routes.append({
                    "host_port": port,
                    "protocol": protocol,
                    "project": project.name,
                    "backend": backend
                })

        return web.json_response(routes)

    async def _api_deploy(self, request: web.Request) -> web.Response:
        """Deploy a project. Accepts either:
          - application/json: {name, source, ref, ...}  (git clone)
          - multipart/form-data: 'manifest' field (JSON) + 'files' field (tarball)
        """
        ct = request.headers.get("Content-Type", "")
        if ct.startswith("multipart/"):
            reader = await request.multipart()
            manifest = None
            files_data = None
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "manifest":
                    try:
                        manifest = json.loads(await part.text())
                    except json.JSONDecodeError as e:
                        return web.json_response({"error": f"manifest is not valid JSON: {e}"}, status=400)
                elif part.name == "files":
                    files_data = await part.read(decode=False)
            if manifest is None:
                return web.json_response({"error": "missing 'manifest' field"}, status=400)
            if files_data is None:
                return web.json_response({"error": "missing 'files' field"}, status=400)
            project = await deploy(
                self.store, self.docker, self.audit_manager, self.tracker, self.rtm,
                manifest, files_data=files_data)
        else:
            manifest = await request.json()
            project = await deploy(
                self.store, self.docker, self.audit_manager, self.tracker, self.rtm, manifest)
        return web.json_response(asdict(project), status=201)

    async def _api_status(self, name: str) -> web.Response:
        project = self.store.load(name)
        return web.json_response(asdict(project))

    async def _api_teardown(self, name: str) -> web.Response:
        await teardown(self.store, self.docker, self.audit_manager, self.tracker,
                       self.rtm, name)
        return web.json_response({"ok": True})

    async def _api_redeploy(self, name: str) -> web.Response:
        project = self.store.load(name)
        old_sha = project.commit_sha
        manifest = {
            "name": project.name, "source": project.source, "ref": project.ref,
            "runtime": project.runtime, "entry": project.entry, "port": project.port,
            "mode": project.mode, "env": project.env,
        }
        if project.listen:
            manifest["listen"] = {
                "port": project.listen.port,
                "protocol": project.listen.protocol,
            }
        project = await deploy(
            self.store, self.docker, self.audit_manager, self.tracker, self.rtm, manifest)
        result = asdict(project)
        result["changed"] = project.commit_sha != old_sha
        return web.json_response(result)

    async def _api_promote(self, name: str) -> web.Response:
        try:
            project = await promote(self.store, self.audit_manager, self.rtm, name)
            return web.json_response(asdict(project))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def _api_audit(self, name: str) -> web.Response:
        """Get audit log for a specific project."""
        try:
            project = self.store.load(name)
            if project.mode != "attested":
                return web.json_response({"error": "project not attested"}, status=400)
            audit = self.audit_manager.get_audit_log(name)
            return web.json_response(audit.to_json())
        except FileNotFoundError:
            return web.json_response({"error": "project not found"}, status=404)

    async def _api_attest(self, name: str) -> web.Response:
        project = self.store.load(name)
        if project.mode != "attested":
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

    async def _api_verification(self, name: str) -> web.Response:
        """Get trust chain data for project verification."""
        try:
            project = self.store.load(name)
            if project.mode != "attested":
                return web.json_response({"error": "project not attested"}, status=400)

            # Get dstack quote
            quote = None
            if DSTACK_SOCK:
                try:
                    key_path = f"/tee-daemon/projects/{name}"
                    body = {"path": key_path}
                    conn = aiohttp.UnixConnector(path=DSTACK_SOCK)
                    async with aiohttp.ClientSession(connector=conn) as session:
                        async with session.post("http://localhost/GetKey", json=body) as resp:
                            if resp.status == 200:
                                quote = await resp.json()
                except Exception as e:
                    log.warning("Failed to get dstack quote: %s", e)

            # Get audit log
            audit = []
            try:
                audit_log = self.audit_manager.get_audit_log(name)
                audit = audit_log.to_json()
            except Exception as e:
                log.warning("Failed to get audit log: %s", e)

            return web.json_response({
                "project": asdict(project),
                "quote": quote,
                "audit": audit,
            })
        except FileNotFoundError:
            return web.json_response({"error": "project not found"}, status=404)

    async def _api_create_tunnel(self, request: web.Request) -> web.Response:
        """Create a new tunnel."""
        try:
            data = await request.json()
            backend = data.get("backend", "")
            timeout = data.get("timeout", 0)
            auth_mode = data.get("auth", "none")

            if not backend:
                return web.json_response({"error": "backend is required"}, status=400)

            # Default timeout to 1 hour
            if not timeout:
                timeout = 3600

            try:
                tunnel = self.tunnel_store.create(backend, timeout, auth_mode)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)

            # Construct the public URL
            # Use request's scheme/host if available, otherwise default
            scheme = request.headers.get("X-Forwarded-Proto", "https")
            host = request.headers.get("X-Forwarded-Host", request.host)

            response = TunnelResponse(
                id=tunnel.id,
                url=f"{scheme}://{host}/t/{tunnel.id}/",
                expires_at=tunnel.expires_at,
                tid=tunnel.tid
            )

            return web.json_response(asdict(response), status=201)
        except Exception as e:
            log.error("Error creating tunnel: %s", e)
            return web.json_response({"error": "internal server error"}, status=500)

    async def _api_list_tunnels(self) -> web.Response:
        """List all active tunnels."""
        tunnels = self.tunnel_store.list()
        return web.json_response([asdict(t) for t in tunnels])

    async def _api_delete_tunnel(self, tunnel_id: str) -> web.Response:
        """Delete a tunnel."""
        if self.tunnel_store.delete(tunnel_id):
            return web.json_response({"ok": True})
        return web.json_response({"error": "tunnel not found"}, status=404)

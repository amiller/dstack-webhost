"""Ingress reverse proxy + management API on port 8080."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from .docker_client import DockerClient
from .projects import ProjectStore
from .tracker import ContainerTracker
from .audit import AuditLogManager, AuditEntry
from .deploy import deploy, teardown, promote, import_bundle
from . import runtimes as runtimes_mod
from .runtimes import RuntimeManager
from .tunnel import TunnelStore, TunnelResponse
from .tokens import DEFAULT_TTL, TokenStore
from .broker import BrokerStore
from .browser_pool import BrowserPool, LeaseTimeout
from . import secp
from . import evidence
from .ladder import ladder_hint

log = logging.getLogger(__name__)


def _sanitize_getkey(data):
    """dstack GetKey returns the derived PRIVATE key (k256 signing key) to the
    in-TEE caller. This endpoint is public, so never echo it — derive and return
    the compressed public key instead, keeping the signature_chain (the actual
    KMS-rooted attestation)."""
    if not isinstance(data, dict) or "key" not in data:
        return data
    try:
        priv = bytes.fromhex(data["key"].replace("0x", ""))[:32]
        out = {k: v for k, v in data.items() if k != "key"}
        out["pubkey"] = secp.compressed_pubkey(priv).hex()
        return out
    except Exception as e:
        log.warning("could not sanitize GetKey response: %s", e)
        return {k: v for k, v in data.items() if k != "key"}


def _redact_env(data: dict) -> dict:
    """Replace every env value with '<redacted>'. API responses — admin or public —
    must never echo plaintext secrets back to the client or into logs; GET, status,
    deploy, redeploy, promote and aggregate all share this one rule."""
    if data.get("env"):
        data["env"] = dict.fromkeys(data["env"], "<redacted>")
    return data


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
                 rtm: RuntimeManager, tunnel_store: TunnelStore,
                 token_store: TokenStore, broker_store: BrokerStore | None = None,
                 browser_pool: BrowserPool | None = None):
        self.store = store
        self.docker = docker
        self.audit_manager = audit_manager
        self.tracker = tracker
        self.rtm = rtm
        self.tunnel_store = tunnel_store
        self.token_store = token_store
        self.broker_store = broker_store
        self.browser_pool = browser_pool
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

        if name in ("about", "_about") and (len(parts) == 1 or not parts[1]):
            template_path = os.path.join(
                os.path.dirname(__file__), "templates", "about.html")
            with open(template_path) as f:
                return web.Response(text=f.read(), content_type="text/html")

        if not name:
            # Public listing. Browsers (Accept: text/html) get the default
            # viewer page; programmatic callers get JSON. Anonymous callers
            # see only attested projects (dev projects do not leak); an
            # authenticated caller sees everything.
            accept = request.headers.get("Accept", "")
            if "text/html" in accept and "application/json" not in accept:
                template_path = os.path.join(
                    os.path.dirname(__file__), "templates", "index.html")
                with open(template_path) as f:
                    return web.Response(text=f.read(), content_type="text/html")
            auth = request.headers.get("Authorization", "")
            authed = (API_TOKEN and auth.startswith("Bearer ")
                      and hmac.compare_digest(auth[7:], API_TOKEN))
            all_projects = self.store.list()
            visible = [p for p in all_projects
                       if authed or p.mode == "attested" or p.public]
            projects = {p.name: {
                "runtime": p.runtime, "mode": p.mode, "public": p.public,
                "source": p.source, "commit_sha": p.commit_sha,
                "tree_hash": p.tree_hash,
                # RFC 0029 layering signal: a measured operator-debug door.
                # Gated to attested at deploy time, so this only ever appears
                # on the public-attested surface — its existence is part of the
                # measurement, never a hidden side channel.
                "operator_debug": p.operator_debug,
            } for p in visible}
            # Count of projects hidden from this viewer — drives the anonymous
            # "pointer to the interesting ones" on the landing page (#43).
            hidden = 0 if authed else len(all_projects) - len(visible)
            resp = web.json_response({"projects": projects, "hidden": hidden})
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

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

        if project.runtime == "image" or project.isolation == "container":
            route = self.rtm.get_image_route(project.name)
            if not route:
                return web.json_response({"error": "image container not running"}, status=503)
            ip, port = route
            routed_path = subpath
            qs = request.query_string
            if qs:
                routed_path += "?" + qs
            return await self._proxy(request, ip, port, routed_path)

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

        if project.runtime == "image" or project.isolation == "container":
            route = self.rtm.get_image_route(project.name)
            if not route:
                return web.json_response({"error": "image container not running"}, status=503)
            ip, port = route
            qs = request.query_string
            if qs:
                subpath += "?" + qs
            return await self._proxy(request, ip, port, subpath)

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

    def _check_auth(self, request: web.Request, api_path: str,
                    owner_only: bool = False) -> web.Response | None:
        if not API_TOKEN:
            return None
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return web.json_response({"error": "missing token"}, status=401)
        token = auth[7:]
        if hmac.compare_digest(token, API_TOKEN):
            return None
        if owner_only:
            return web.json_response({"error": "owner token required"}, status=403)
        if not self.token_store.authenticate(token, api_path):
            return web.json_response({"error": "invalid token or scope"}, status=403)
        return None

    def _substrate_info(self) -> dict:
        rt = runtimes_mod.CONTAINER_RUNTIME
        shim_sha = hashlib.sha256(
            runtimes_mod._ENTRY_SHIM_DENO.encode()).hexdigest()
        return {
            "container_runtime": rt,
            "effective_runtime": rt or "runc",
            "isolation_modes": ["shared", "container"],
            "deno_entry_shim_sha256": shim_sha,
            "networks": [runtimes_mod.NETWORK_DEV, runtimes_mod.NETWORK_ATTESTED],
        }

    def _api_version(self) -> dict:
        """Return daemon version and git commit. Identity is resolved and
        validated once at boot (proxy.main._resolve_commit): baked from the
        build arg in an image, read from git when running from a checkout.
        No request-time fallback — it could only mask a broken deploy."""
        return {
            "version": os.environ.get("DAEMON_VERSION", "dev"),
            "commit": os.environ["DAEMON_COMMIT"],
        }

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
        # Public responses include CORS headers so browser-based verifiers work.
        if method == "OPTIONS" and self._public_attested_path(path) is not None:
            return web.Response(status=204, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Max-Age": "86400",
            })

        if method == "GET" and path == "version":
            resp = web.json_response(self._api_version())
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        if method == "GET" and path == "substrate":
            resp = web.json_response(self._substrate_info())
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        if method == "GET":
            public_name = self._public_attested_path(path)
            if public_name is not None:
                try:
                    project = self.store.load(public_name)
                except FileNotFoundError:
                    resp = web.json_response({"error": "not found"}, status=404)
                    resp.headers["Access-Control-Allow-Origin"] = "*"
                    return resp
                if project.mode == "attested":
                    if path.startswith("attest/"):
                        resp = await self._api_attest(public_name)
                    elif path.startswith("verification/"):
                        resp = await self._api_verification(request, public_name)
                    elif path.endswith("/audit"):
                        resp = await self._api_audit(public_name)
                    else:
                        resp = await self._api_status(public_name)
                    resp.headers["Access-Control-Allow-Origin"] = "*"
                    return resp

        owner_only = path == "tokens" or path.startswith("tokens/")
        denied = self._check_auth(request, path, owner_only=owner_only)
        if denied:
            return denied

        # Scoped token API endpoints
        if path == "tokens" and method == "POST":
            return await self._api_create_token(request)
        if path == "tokens" and method == "GET":
            return await self._api_list_tokens()
        if path.startswith("tokens/") and method == "DELETE":
            token_id = path.split("/")[1]
            return await self._api_revoke_token(token_id)

        # Tunnel API endpoints
        if path == "tunnels" and method == "POST":
            return await self._api_create_tunnel(request)
        if path == "tunnels" and method == "GET":
            return await self._api_list_tunnels()
        if path.startswith("tunnels/") and method == "DELETE":
            tunnel_id = path.split("/")[1]
            return await self._api_delete_tunnel(tunnel_id)

        # Grant API endpoints (RFC 0018 credential broker)
        if path == "grants" and method == "POST":
            return await self._api_create_grant(request)
        if path == "grants" and method == "GET":
            return await self._api_list_grants(request)
        if path.startswith("grants/") and method == "DELETE":
            grant_id = path.split("/")[1]
            return await self._api_revoke_grant(grant_id)
        if path.startswith("grants/") and path.endswith("/reauthorize") and method == "POST":
            grant_id = path.split("/")[1]
            return await self._api_reauthorize_grant(request, grant_id)
        if path.startswith("grants/") and path.endswith("/usage") and method == "GET":
            grant_id = path.split("/")[1]
            return await self._api_grant_usage(grant_id)

        if path == "routes" and method == "GET":
            return await self._api_routes()

        # RFC 0017: fleet export/import (authed)
        if path == "export" and method == "GET":
            return await self._api_export()

        if path == "import" and method == "POST":
            return await self._api_import(request)

        if path == "audit" and method == "GET":
            return await self._api_all_audit()

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
            return await self._api_verification(request, name)

        # RFC 0028: browser render pool (authed)
        if path == "browser/render" and method == "POST":
            return await self._api_browser_render(request)
        if path == "browser/pool" and method == "GET":
            return self._api_browser_pool_status()

        # RFC 0016: aggregate status endpoint (authed)
        if path == "status" and method == "GET":
            return await self._api_aggregate_status()

        # RFC 0016: console page (authed)
        if path == "console" and method == "GET":
            return await self._api_console()

        return web.json_response({"error": "not found"}, status=404)

    async def _api_list(self) -> web.Response:
        projects = self.store.list()
        return web.json_response([_redact_env(asdict(p)) for p in projects])

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
                liveness = self.rtm.get_project_liveness(project)
                routes.append({
                    "host_port": port,
                    "protocol": protocol,
                    "project": project.name,
                    "backend": liveness["backend"]
                })

        return web.json_response(routes)

    async def _api_deploy(self, request: web.Request) -> web.Response:
        """Deploy a project. Accepts either:
          - application/json: {name, source, ref, ...}  (git clone)
          - multipart/form-data: 'manifest' field (JSON) + 'files' field (tarball)
        """
        ct = request.headers.get("Content-Type", "")
        try:
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
            return web.json_response(_redact_env(asdict(project)), status=201)
        except ValueError as e:
            # Bad manifest / port conflict etc. — the message is safe to return.
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            import traceback
            log.error("deploy failed: %s", traceback.format_exc())
            return web.json_response({"error": str(e)}, status=500)

    async def _api_export(self) -> web.Response:
        """RFC 0017 §1: pin bundle for every project + audit refs. Raw env
        secrets are excluded by the export projection (RFC 0018 owns them)."""
        projects = self.store.list()
        audit = {}
        for p in projects:
            entries = self.audit_manager.get_audit_log(p.name).to_json()
            audit[p.name] = {"entries": len(entries),
                             "url": f"/_api/projects/{p.name}/audit"}
        return web.json_response({
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "projects": [p.export_dict() for p in projects],
            "audit": audit,
        })

    async def _api_import(self, request: web.Request) -> web.Response:
        """RFC 0017 §2: redeploy each bundle entry pinned; a pin that cannot be
        reproduced errors and skips that project (never re-clones at latest)."""
        try:
            bundle = await request.json()
        except json.JSONDecodeError as e:
            return web.json_response({"error": f"bundle is not valid JSON: {e}"}, status=400)
        try:
            result = await import_bundle(
                self.store, self.docker, self.audit_manager,
                self.tracker, self.rtm, bundle)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(result)

    async def _api_status(self, name: str) -> web.Response:
        project = self.store.load(name)
        return web.json_response(_redact_env(asdict(project)))

    async def _api_teardown(self, name: str) -> web.Response:
        await teardown(self.store, self.docker, self.audit_manager, self.tracker,
                       self.rtm, name)
        return web.json_response({"ok": True})

    async def _api_redeploy(self, name: str) -> web.Response:
        project = self.store.load(name)
        old_sha = project.commit_sha
        old_digest = project.image_digest
        manifest = {
            "name": project.name, "source": project.source, "ref": project.ref,
            "runtime": project.runtime, "entry": project.entry, "port": project.port,
            "mode": project.mode, "env": project.env,
            "isolation": project.isolation,
            "image": project.image, "image_port": project.image_port,
            "volumes": project.volumes, "env_passthrough": project.env_passthrough,
            "dstack_env": project.dstack_env,
            "oci_runtime": project.oci_runtime,
        }
        if project.listen:
            manifest["listen"] = {
                "port": project.listen.port,
                "protocol": project.listen.protocol,
            }
        project = await deploy(
            self.store, self.docker, self.audit_manager, self.tracker, self.rtm, manifest)
        result = _redact_env(asdict(project))
        if project.runtime == "image":
            result["changed"] = project.image_digest != old_digest
        else:
            result["changed"] = project.commit_sha != old_sha
        return web.json_response(result)

    async def _api_promote(self, name: str) -> web.Response:
        try:
            project = await promote(self.store, self.audit_manager, self.rtm, name)
            return web.json_response(_redact_env(asdict(project)))
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

    async def _api_all_audit(self) -> web.Response:
        """Get audit entries for all known projects."""
        entries: list[dict] = []
        for project in self.store.list():
            audit = self.audit_manager.get_audit_log(project.name)
            entries.extend(audit.to_json())
        entries.sort(key=lambda e: e.get("timestamp", 0))
        return web.json_response(entries)

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
                return web.json_response(_sanitize_getkey(data), status=resp.status)

    async def _api_verification(self, request: web.Request, name: str) -> web.Response:
        """Get the RFC 0020 evidence bundle for project verification.

        RFC 0016: supports ?format=html for a human-readable rendering.
        """
        try:
            project = self.store.load(name)
            if project.mode != "attested":
                return web.json_response({"error": "project not attested"}, status=400)

            # HTML rendering (RFC 0016 human view)
            if request.query.get("format", "") == "html":
                return await self._render_verification_html(request, project)

            # Build RFC 0020 Evidence Bundle (machine-verifiable, no verdict)
            bundle = evidence.EvidenceBundle()
            platform_quote = {}
            binding_quote = {}
            if DSTACK_SOCK:
                try:
                    key_path = f"/tee-daemon/projects/{name}"
                    conn = aiohttp.UnixConnector(path=DSTACK_SOCK)
                    async with aiohttp.ClientSession(connector=conn) as session:
                        # GetQuote for TDX platform quote
                        try:
                            body = {"path": key_path}
                            async with session.post("http://localhost/GetQuote", json=body) as resp:
                                if resp.status == 200:
                                    platform_quote = await resp.json()
                        except Exception as e:
                            log.warning("GetQuote failed: %s", e)

                        # GetKey for signature chain (KMS-rooted attestation)
                        async with session.post("http://localhost/GetKey", json={"path": key_path}) as resp:
                            if resp.status == 200:
                                binding_quote = _sanitize_getkey(await resp.json())
                except Exception as e:
                    log.warning("Failed to get dstack quotes: %s", e)

            bundle.platform_quote = platform_quote
            bundle.webhost_app_id = os.environ.get("WEBHOST_APP_ID", "")

            # On-chain info (MVP: chain_id 0 for non-anchored, empty addresses)
            # In production: populate from base-prod RPC and contract addresses
            bundle.onchain = evidence.OnchainInfo(
                chain_id=0,  # MVP: non-anchored (pha-prod7)
                kms_contract="",
                dstackapp="",
                allowed_compose_hash="",
                allowed_os_image="",
            )

            # Gateway info (MVP: empty, to be populated from pinned gateway refs)
            bundle.gateway = evidence.GatewayInfo(
                domain="",
                app_id="",
                zt_cert_ref="",
            )

            # App info
            bundle.app = evidence.AppInfo(
                project=project.name,
                source=evidence.SourceInfo(
                    repo=project.source or "",
                    ref=project.ref or "",
                    commit_sha=project.commit_sha or "",
                    tree_hash=project.tree_hash or "",
                    tree_hash_kind="git" if project.source else "sha256",
                ),
                image_digest=project.image_digest or "",
                binding_quote=binding_quote,
                operator_debug=evidence.OperatorDebugInfo(
                    enabled=bool(project.operator_debug),
                ),
            )

            # Attach the per-project audit log to the bundle (part of the served shape).
            try:
                audit_log = self.audit_manager.get_audit_log(name)
                bundle.audit = audit_log.to_json()
            except Exception as e:
                log.warning("Failed to get audit log: %s", e)

            return web.json_response(bundle.to_dict())
        except FileNotFoundError:
            return web.json_response({"error": "project not found"}, status=404)

    async def _render_verification_html(self, request: web.Request, project) -> web.Response:
        """Render the verification bundle as an HTML page."""
        # Get dstack quote
        quote = None
        if DSTACK_SOCK:
            try:
                key_path = f"/tee-daemon/projects/{project.name}"
                body = {"path": key_path}
                conn = aiohttp.UnixConnector(path=DSTACK_SOCK)
                async with aiohttp.ClientSession(connector=conn) as session:
                    async with session.post("http://localhost/GetKey", json=body) as resp:
                        if resp.status == 200:
                            quote = _sanitize_getkey(await resp.json())
            except Exception as e:
                log.warning("Failed to get dstack quote: %s", e)

        # Get audit log
        audit = []
        try:
            audit_log = self.audit_manager.get_audit_log(project.name)
            audit = audit_log.to_json()
        except Exception as e:
            log.warning("Failed to get audit log: %s", e)

        # Build JSON data for the template
        verification_data = {
            "project": _redact_env(asdict(project)),
            "quote": quote,
            "audit": audit,
        }

        # Read and render template
        template_path = os.path.join(
            os.path.dirname(__file__), "templates", "verification.html")
        with open(template_path, "r") as f:
            template = f.read()

        html = template.replace("{{ project_name }}", project.name)

        # Add data as JSON in a script tag for the template to use
        data_script = f'window.verificationData = {json.dumps(verification_data)};'
        html = html.replace(
            '<script>',
            f'<script>{data_script}\n        '
        )

        resp = web.Response(text=html, content_type="text/html")
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

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

    async def _api_create_token(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            scope = data.get("scope", "")
            ttl = data.get("ttl", DEFAULT_TTL)
            try:
                ttl = int(ttl)
            except (TypeError, ValueError):
                return web.json_response({"error": "ttl must be an integer"}, status=400)
            token, bearer = self.token_store.create(scope, ttl)
            body = token.public()
            body["token"] = bearer
            return web.json_response(body, status=201)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            log.error("Error creating scoped API token: %s", e)
            return web.json_response({"error": "internal server error"}, status=500)

    async def _api_list_tokens(self) -> web.Response:
        return web.json_response([t.public() for t in self.token_store.list()])

    async def _api_revoke_token(self, token_id: str) -> web.Response:
        if self.token_store.revoke(token_id):
            return web.json_response({"ok": True})
        return web.json_response({"error": "token not found"}, status=404)

    async def _api_create_grant(self, request: web.Request) -> web.Response:
        """Create a new credential grant (RFC 0018)."""
        if not self.broker_store:
            return web.json_response({"error": "broker not available"}, status=503)
        try:
            data = await request.json()
            project = data.get("project", "")
            name = data.get("name", "")
            scope = data.get("scope", "")
            upstream = data.get("upstream", {})
            secret = data.get("secret", "")
            ttl = data.get("ttl")
            mode = data.get("mode", "proxy")

            # Validate required fields
            if not project:
                return web.json_response({"error": "project is required"}, status=400)
            if not name:
                return web.json_response({"error": "name is required"}, status=400)
            if not upstream or not upstream.get("base_url"):
                return web.json_response({"error": "upstream.base_url is required"}, status=400)
            if not secret:
                return web.json_response({"error": "secret is required"}, status=400)

            # Verify project exists
            try:
                self.store.load(project)
            except FileNotFoundError:
                return web.json_response({"error": "project not found"}, status=404)

            # Validate upstream config
            if not upstream.get("allow_paths"):
                upstream["allow_paths"] = ["/*"]
            if not upstream.get("allow_methods"):
                upstream["allow_methods"] = ["POST"]
            if not upstream.get("inject"):
                upstream["inject"] = {"header": "Authorization", "template": "Bearer {secret}"}

            # Create grant
            grant = await self.broker_store.create(
                project=project,
                name=name,
                scope=scope,
                upstream=upstream,
                secret=secret,
                ttl=ttl,
                mode=mode
            )

            # Audit log
            await self.audit_manager.get_audit_log(project).record(AuditEntry(
                timestamp=time.time(), action="grant",
                detail=json.dumps({"grant_id": grant.id, "name": name})))

            return web.json_response({
                "id": grant.id,
                "expires_at": grant.expires_at
            }, status=201)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=503)
        except Exception as e:
            log.error("Error creating grant: %s", e)
            return web.json_response({"error": "internal server error"}, status=500)

    async def _api_list_grants(self, request: web.Request) -> web.Response:
        """List grants, optionally filtered by project."""
        if not self.broker_store:
            return web.json_response({"error": "broker not available"}, status=503)
        project = request.query.get("project")
        grants = self.broker_store.list(project=project)
        return web.json_response([g.to_json() for g in grants])

    async def _api_revoke_grant(self, grant_id: str) -> web.Response:
        """Revoke a grant (immediate effect)."""
        if not self.broker_store:
            return web.json_response({"error": "broker not available"}, status=503)
        grant = self.broker_store.get(grant_id)
        if not grant:
            return web.json_response({"error": "grant not found"}, status=404)

        project = grant.project
        if self.broker_store.revoke(grant_id):
            # Audit log
            await self.audit_manager.get_audit_log(project).record(AuditEntry(
                timestamp=time.time(), action="revoke",
                detail=json.dumps({"grant_id": grant_id})))
            return web.json_response({"ok": True})
        return web.json_response({"error": "grant not found"}, status=404)

    async def _api_reauthorize_grant(self, request: web.Request, grant_id: str) -> web.Response:
        """Reauthorize a grant: rotate secret, extend TTL, or change scope."""
        if not self.broker_store:
            return web.json_response({"error": "broker not available"}, status=503)
        try:
            data = await request.json()
            secret = data.get("secret")
            ttl = data.get("ttl")
            scope = data.get("scope")

            grant = await self.broker_store.reauthorize(
                grant_id=grant_id,
                secret=secret,
                ttl=ttl,
                scope=scope
            )

            if not grant:
                return web.json_response({"error": "grant not found"}, status=404)

            # Audit log
            await self.audit_manager.get_audit_log(grant.project).record(AuditEntry(
                timestamp=time.time(), action="reauthorize",
                detail=json.dumps({"grant_id": grant_id})))

            return web.json_response({"ok": True, "expires_at": grant.expires_at})
        except Exception as e:
            log.error("Error reauthorizing grant: %s", e)
            return web.json_response({"error": "internal server error"}, status=500)

    async def _api_grant_usage(self, grant_id: str) -> web.Response:
        """Get recent usage for a grant."""
        if not self.broker_store:
            return web.json_response({"error": "broker not available"}, status=503)
        grant = self.broker_store.get(grant_id)
        if not grant:
            return web.json_response({"error": "grant not found"}, status=404)
        usage = self.broker_store.get_usage(grant_id)
        return web.json_response(usage)

    async def _api_aggregate_status(self) -> web.Response:
        """RFC 0016: Aggregate status for all projects with liveness.

        Returns manifest fields + live liveness (running, container_id, backend).
        For attested projects, includes the public verification URL.
        """
        projects = []
        for project in self.store.list():
            data = _redact_env(asdict(project))
            # Add liveness info
            liveness = self.rtm.get_project_liveness(project)
            data["running"] = liveness["running"]
            data["container_id"] = liveness["container_id"]
            data["backend"] = liveness["backend"]
            data["ladder"] = ladder_hint(data)
            # For attested projects, include public verification URL
            if project.mode == "attested":
                # Get the scheme and host from the environment or request
                data["verification_url"] = f"/_api/verification/{project.name}"
            projects.append(data)
        return web.json_response(projects)

    async def _api_console(self) -> web.Response:
        """RFC 0016: Serve the fleet console HTML page."""
        console_path = os.path.join(
            os.path.dirname(__file__), "templates", "console.html")
        try:
            with open(console_path, "r") as f:
                return web.Response(text=f.read(), content_type="text/html")
        except FileNotFoundError:
            return web.json_response({"error": "console not found"}, status=404)

    def _api_browser_pool_status(self) -> web.Response:
        """RFC 0028: pool liveness (slots free/busy, active leases)."""
        if not self.browser_pool:
            return web.json_response({"error": "browser pool not available"}, status=503)
        return web.json_response(self.browser_pool.status())

    async def _api_browser_render(self, request: web.Request) -> web.Response:
        """RFC 0028: one-shot lease -> drive -> release. Acquires a browser from
        the pool, injects the requester's jar for `domain` only, renders `url`,
        and resets the container before it returns to the pool — so concurrent
        callers never see each other's session."""
        if not self.browser_pool:
            return web.json_response({"error": "browser pool not available"}, status=503)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        domain = data.get("domain", "")
        jar = data.get("jar", "")
        url = data.get("url", "")
        if not url:
            return web.json_response({"error": "url is required"}, status=400)
        timeout = float(data.get("timeout") or 0) or None
        try:
            result = await self.browser_pool.render(domain, jar, url, timeout=timeout)
        except LeaseTimeout as e:
            return web.json_response({"error": "lease timeout", "detail": str(e)},
                                     status=503)
        except Exception as e:
            log.error("browser render failed: %s", e)
            return web.json_response({"error": "render failed", "detail": str(e)},
                                     status=502)
        return web.json_response(result)

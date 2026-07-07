"""dstack socket proxy — filters dstack JSON-RPC API requests.

Each proxy instance is scoped to ONE project (``project_id``). ``GetKey`` never
trusts a caller-supplied ``path``: it reads a ``name`` (a single safe path
component) and constructs the key path server-side as
``/tee-daemon/projects/<project_id>/<name>``. The daemon serves one such proxy
per project on its own socket (see :class:`DstackProxyManager`), so a tenant can
only ever derive its OWN project's keys — closing the cross-tenant derivation
flaw (#7). A project's container is mounted only its own broker dir, so it
cannot even reach another project's socket.
"""

import json
import logging
import os

import aiohttp
from aiohttp import web

log = logging.getLogger(__name__)

ALLOWED_METHODS = {"GetTlsKey", "GetQuote", "Info", "EmitEvent", "GetKey"}
PROJECT_KEY_PREFIX = "/tee-daemon/projects/"


def _valid_key_name(name: str) -> bool:
    """A GetKey ``name`` must be a single safe path component (no traversal)."""
    if not name:
        return False
    return not ("/" in name or "\\" in name or ".." in name or name.startswith("."))


class DstackProxy:
    def __init__(self, real_socket: str, project_id: str):
        self.real_socket = real_socket
        self.project_id = project_id

    async def handle(self, request: web.Request) -> web.Response:
        body_bytes = await request.read()
        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except json.JSONDecodeError:
            return web.json_response({"message": "Invalid JSON"}, status=400)

        method = request.path.lstrip("/")
        if not method:
            method = body.get("method", "")

        if method not in ALLOWED_METHODS:
            log.warning("Denied dstack method: %s", method)
            return web.json_response({"message": f"Method {method} not permitted"}, status=403)

        if method == "GetKey":
            # The key path is derived from THIS proxy's bound project identity,
            # never from a caller-supplied field — a tenant cannot name another
            # project's key path (issue #7). Any legacy `path` is ignored.
            name = body.get("name", "")
            if not _valid_key_name(name):
                log.warning("Denied GetKey for project %s with bad name: %r",
                            self.project_id, name)
                return web.json_response(
                    {"message": "GetKey requires a `name` (no '/', '\\', '..', or leading '.')"},
                    status=400,
                )
            body["path"] = f"{PROJECT_KEY_PREFIX}{self.project_id}/{name}"
            body_bytes = json.dumps(body).encode()

        return await self._forward(request.method, request.path, body_bytes, request.headers)

    async def _forward(self, method: str, path: str, body: bytes, headers) -> web.Response:
        # Drop hop-by-hop headers. Content-Length is dropped because GetKey rewrites
        # the body (a stale length would truncate the forwarded request).
        fwd_headers = {k: v for k, v in headers.items()
                       if k.lower() not in ("host", "transfer-encoding", "content-length")}
        conn = aiohttp.UnixConnector(path=self.real_socket)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.request(method, f"http://localhost{path}",
                                       data=body if body else None,
                                       headers=fwd_headers) as resp:
                resp_body = await resp.read()
                return web.Response(
                    body=resp_body,
                    status=resp.status,
                    content_type=resp.content_type,
                )


class DstackProxyManager:
    """Serves one project-scoped :class:`DstackProxy` per project, plus the
    shared ``BrokerProxy`` (``creds.sock``), each inside its own subdirectory of
    ``broker_socket_dir``. A project's container is mounted ONLY its own subdir,
    so it sees ``/run/broker/dstack.sock`` and ``/run/broker/creds.sock`` and
    nothing else — it cannot reach another project's sockets."""

    def __init__(self, real_socket: str, broker_socket_dir: str,
                 broker_proxy_runner=None):
        self.real_socket = real_socket
        self.broker_socket_dir = broker_socket_dir
        self.broker_proxy_runner = broker_proxy_runner  # shared creds.sock AppRunner, or None
        # project -> (dstack AppRunner, [BaseSite, ...])
        self._projects: dict[str, tuple[web.AppRunner, list]] = {}

    async def ensure(self, project_id: str):
        if project_id in self._projects:
            return
        pdir = os.path.join(self.broker_socket_dir, project_id)
        os.makedirs(pdir, exist_ok=True)

        proxy = DstackProxy(self.real_socket, project_id)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", proxy.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        sites: list = []

        dstack_sock = os.path.join(pdir, "dstack.sock")
        if os.path.exists(dstack_sock):
            os.unlink(dstack_sock)
        site = web.UnixSite(runner, dstack_sock)
        await site.start()
        os.chmod(dstack_sock, 0o666)
        sites.append(site)

        # creds.sock is token-authenticated per call (BrokerProxy), so the same
        # runner is safe to serve on every project's socket. Riding the project
        # subdir keeps a single /run/broker mount for both sockets.
        if self.broker_proxy_runner is not None:
            creds_sock = os.path.join(pdir, "creds.sock")
            if os.path.exists(creds_sock):
                os.unlink(creds_sock)
            csite = web.UnixSite(self.broker_proxy_runner, creds_sock)
            await csite.start()
            os.chmod(creds_sock, 0o666)
            sites.append(csite)

        self._projects[project_id] = (runner, sites)
        log.info("Project broker for %s at %s/dstack.sock", project_id, pdir)

    async def remove(self, project_id: str):
        entry = self._projects.pop(project_id, None)
        if not entry:
            return
        runner, sites = entry
        for s in sites:
            try:
                await s.stop()
            except Exception as e:
                log.debug("stop site for %s: %s", project_id, e)
        await runner.cleanup()
        pdir = os.path.join(self.broker_socket_dir, project_id)
        for fn in ("dstack.sock", "creds.sock"):
            try:
                os.unlink(os.path.join(pdir, fn))
            except FileNotFoundError:
                pass

    async def stop_all(self):
        for pid in list(self._projects):
            await self.remove(pid)

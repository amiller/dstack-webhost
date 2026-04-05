"""dstack socket proxy — filters dstack JSON-RPC API requests."""

import json
import logging

import aiohttp
from aiohttp import web

log = logging.getLogger(__name__)

ALLOWED_METHODS = {"GetTlsKey", "GetQuote", "Info", "EmitEvent", "GetKey"}
KEY_PATH_PREFIX = "/tee-daemon/"


class DstackProxy:
    def __init__(self, real_socket: str):
        self.real_socket = real_socket

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
            path = body.get("path", "")
            if not path.startswith(KEY_PATH_PREFIX):
                log.warning("Denied GetKey with path: %s", path)
                return web.json_response(
                    {"message": f"GetKey path must start with {KEY_PATH_PREFIX}"},
                    status=403,
                )

        return await self._forward(request.method, request.path, body_bytes, request.headers)

    async def _forward(self, method: str, path: str, body: bytes, headers) -> web.Response:
        fwd_headers = {k: v for k, v in headers.items()
                       if k.lower() not in ("host", "transfer-encoding")}
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

"""GET /_api/logs and /_api/logs/<container>: owner-only, streamed container logs."""

import asyncio
import tempfile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from . import ingress
from .docker_client import DockerClient
from .ingress import Ingress


def _frame(stream, data):
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


class FakeDocker:
    def __init__(self):
        self.calls = []

    async def list_all_containers(self):
        return [{"Names": ["/dstack-tee-daemon-1"], "State": "running", "Status": "Up 7 days",
                 "Image": "ghcr.io/amiller/tee-socket-proxy@sha256:ab"}]

    async def stream_logs(self, name, params):
        self.calls.append((name, params))
        if name != "dstack-tee-daemon-1":
            raise LookupError(f"no such container: {name}")
        yield b"line 1\n"
        yield b"line 2\n"


class FakeTokens:
    def authenticate(self, token, path):
        return True  # a scoped token that would pass any scope check


async def _get(docker, url, token):
    ing = Ingress(None, docker, None, None, None, None, FakeTokens())
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", ing.handle)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        return resp.status, await resp.text()


def test_owner_reads_full_history(monkeypatch):
    monkeypatch.setattr(ingress, "API_TOKEN", "owner")
    docker = FakeDocker()
    status, body = asyncio.run(_get(docker, "/_api/logs/dstack-tee-daemon-1?since=1730419200", "owner"))
    assert (status, body) == (200, "line 1\nline 2\n")
    assert docker.calls == [("dstack-tee-daemon-1", {"stdout": "true", "stderr": "true",
                             "timestamps": "true", "tail": "all", "since": "1730419200"})]


def test_list(monkeypatch):
    monkeypatch.setattr(ingress, "API_TOKEN", "owner")
    status, body = asyncio.run(_get(FakeDocker(), "/_api/logs", "owner"))
    assert status == 200 and '"name": "dstack-tee-daemon-1"' in body


def test_scoped_token_refused(monkeypatch):
    monkeypatch.setattr(ingress, "API_TOKEN", "owner")
    for url in ("/_api/logs", "/_api/logs/dstack-tee-daemon-1"):
        assert asyncio.run(_get(FakeDocker(), url, "scoped"))[0] == 403


def test_bad_input_and_missing(monkeypatch):
    monkeypatch.setattr(ingress, "API_TOKEN", "owner")
    for url in ("/_api/logs/..%2Fimages%2Fjson", "/_api/logs/a?tail=-1",
                "/_api/logs/a?since=yesterday", "/_api/logs/a?until=1%26follow%3Dtrue"):
        assert asyncio.run(_get(FakeDocker(), url, "owner"))[0] == 400, url
    assert asyncio.run(_get(FakeDocker(), "/_api/logs/nope", "owner"))[0] == 404


async def _engine(sock, tty, body):
    async def inspect(req):
        if req.match_info["name"] != "c":
            return web.json_response({"message": "No such container"}, status=404)
        return web.json_response({"Config": {"Tty": tty}})

    async def logs(req):
        resp = web.StreamResponse()
        await resp.prepare(req)
        for i in range(0, len(body), 5):  # split frames across writes
            await resp.write(body[i:i + 5])
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/containers/{name}/json", inspect)
    app.router.add_get("/containers/{name}/logs", logs)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.UnixSite(runner, sock).start()
    return runner


async def _collect(tty, body, name="c"):
    with tempfile.TemporaryDirectory() as d:
        sock = f"{d}/docker.sock"
        runner = await _engine(sock, tty, body)
        try:
            return b"".join([c async for c in DockerClient(sock).stream_logs(name, {"tail": "all"})])
        finally:
            await runner.cleanup()


def test_stream_logs_demuxes_real_framing():
    framed = _frame(1, b"out a\n") + _frame(2, b"err b\n") + _frame(1, b"x" * 70000)
    assert asyncio.run(_collect(False, framed)) == b"out a\nerr b\n" + b"x" * 70000


def test_stream_logs_tty_is_raw():
    assert asyncio.run(_collect(True, b"raw tty bytes\n")) == b"raw tty bytes\n"


def test_stream_logs_missing_container():
    try:
        asyncio.run(_collect(False, b"", name="nope"))
    except LookupError:
        return
    raise AssertionError("expected LookupError")

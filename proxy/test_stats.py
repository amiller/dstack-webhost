"""GET /_api/stats (issue #151): per-container memory with the apps each hosts."""

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from . import ingress
from .ingress import Ingress


def _stats(usage, inactive, shmem=0):
    return {
        "memory_stats": {"usage": usage, "limit": 4 << 30, "stats": {
            "inactive_file": inactive, "anon": usage - inactive - shmem,
            "file": inactive + shmem, "shmem": shmem}},
        "cpu_stats": {"cpu_usage": {"total_usage": 3_000}, "system_cpu_usage": 20_000, "online_cpus": 4},
        "precpu_stats": {"cpu_usage": {"total_usage": 1_000}, "system_cpu_usage": 10_000},
    }


class FakeDocker:
    async def list_containers(self, label):
        assert label == "tee-proxy.managed=true"
        return [
            {"Id": "aaaa" * 16, "Names": ["/tee-runtime-deno-dev"], "Labels": {
                "tee-proxy.managed": "true", "tee-daemon.runtime": "deno",
                "tee-daemon.project.beta": "true", "tee-daemon.project.alpha": "true"}},
            {"Id": "bbbb" * 16, "Names": ["/tee-image-vpn-attested"], "Labels": {
                "tee-proxy.managed": "true", "tee-daemon.project.vpn": "true"}},
        ]

    async def stats(self, cid):
        return {"aaaa" * 16: _stats(300, 100), "bbbb" * 16: _stats(900, 50, shmem=600),
                "daemonhost": _stats(500, 0)}[cid]


class FakeTokens:
    def authenticate(self, token, path):
        return True


async def _get(headers):
    ing = Ingress(None, FakeDocker(), None, None, None, None, FakeTokens())
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", ing.handle)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/_api/stats", headers=headers)
        return resp.status, await resp.json()


def test_stats(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "daemonhost")
    monkeypatch.setattr(ingress, "API_TOKEN", "owner")
    status, rows = asyncio.run(_get({"Authorization": "Bearer owner"}))
    assert status == 200
    assert [r["container"] for r in rows] == ["tee-image-vpn-attested", "tee-daemon", "tee-runtime-deno-dev"]
    vpn, daemon, deno = rows
    assert vpn == {"container": "tee-image-vpn-attested", "id": "bbbb" * 3, "apps": ["vpn"],
                   "usage": 900, "limit": 4 << 30, "working_set": 850, "anon": 250,
                   "file": 650, "shmem": 600, "cpu_percent": 80.0}
    assert deno["apps"] == ["alpha", "beta"] and deno["working_set"] == 200
    assert daemon["apps"] == [] and daemon["id"] == "daemonhost"


def test_stats_owner_only(monkeypatch):
    monkeypatch.setattr(ingress, "API_TOKEN", "owner")
    status, _ = asyncio.run(_get({"Authorization": "Bearer scoped"}))
    assert status == 403

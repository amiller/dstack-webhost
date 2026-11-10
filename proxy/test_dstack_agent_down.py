"""A dead dstack guest agent reaches the app as a named 503, not an anonymous 500."""

import asyncio
import os
import socket
import tempfile

import aiohttp
from aiohttp import web

from .dstack_proxy import DstackProxy


def test_getkey_with_dead_guest_agent_is_named_503():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            dead = os.path.join(tmp, "dstack.sock")
            # A bound, non-listening socket refuses connections like the dead agent's did.
            s = socket.socket(socket.AF_UNIX)
            s.bind(dead)
            app = web.Application()
            app.router.add_route("*", "/{path:.*}", DstackProxy(dead, "projA").handle)
            runner = web.AppRunner(app)
            await runner.setup()
            front = os.path.join(tmp, "proxy.sock")
            await web.UnixSite(runner, front).start()
            async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=front)) as c:
                async with c.post("http://localhost/GetKey", json={"name": "k"}) as r:
                    assert r.status == 503
                    assert "dstack guest agent unreachable" in (await r.json())["message"]
            await runner.cleanup()
            s.close()

    asyncio.run(run())

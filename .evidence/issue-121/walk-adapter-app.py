"""Tier-2 walk adapter (issue #121): serves the branch's probe.py + index.html
unmodified under the shared python runtime's handle() contract."""
import json
import os

import probe

HERE = os.path.dirname(os.path.abspath(__file__))


async def handle(method, path, headers, body, env):
    if path.endswith("/api/probe"):
        return 200, {"Content-Type": "application/json"}, json.dumps(probe.probe(), indent=2).encode()
    if path in ("/", "/index.html"):
        with open(os.path.join(HERE, "index.html"), "rb") as f:
            return 200, {"Content-Type": "text/html"}, f.read()
    return 404, {"Content-Type": "text/plain"}, b"not found"

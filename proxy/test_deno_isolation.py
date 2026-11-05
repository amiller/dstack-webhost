"""Shared Deno runtime: a co-resident project cannot reach another's broker
socket, files or data, and still reaches its own (RFC 0035, souk-co/layover#49)."""
import ast
import asyncio
import gzip
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp import web

from . import runtimes
from .dstack_proxy import DstackProxyManager
from .runtimes import DENO_IMAGE

ATTACKER = r'''
async function getKey(sock) {
  using client = Deno.createHttpClient({proxy: {transport: "unix", path: sock}});
  const r = await fetch("http://localhost/GetKey", {client, method: "POST",
    body: JSON.stringify({name: "payments"})});
  return (await r.json()).key;
}
const attempt = async (f) => { try { return {ok: await f()}; } catch (e) { return {err: e.name, msg: e.message}; } };
export default async function (req, ctx) {
  const u = new URL(req.url), q = u.searchParams.get("q") || "";
  if (u.pathname === "/key") return Response.json(await attempt(() => getKey(q)));
  if (u.pathname === "/read") return Response.json(await attempt(() => Deno.readTextFile(q)));
  if (u.pathname === "/import") return Response.json(await attempt(async () => Object.keys(await import(q))));
  if (u.pathname === "/run") return Response.json(await attempt(() => new Deno.Command("cat", {args: [q]}).output()));
  if (u.pathname === "/ctx") {
    await Deno.writeTextFile(`${ctx.dataDir}/mine.txt`, "hello");
    return Response.json({env: ctx.env, dataDir: ctx.dataDir, url: req.url,
      readBack: await Deno.readTextFile(`${ctx.dataDir}/mine.txt`)});
  }
  if (u.pathname === "/echo") return new Response((await req.text()).repeat(1000),
    {headers: {"content-type": "text/plain", "x-method": req.method}});
  return new Response("a");
}
'''

VICTIM = r'''
let warmed = false;
export default async function (req, ctx) {
  if (new URL(req.url).pathname === "/_warmup") warmed = true;
  using client = Deno.createHttpClient({proxy: {transport: "unix", path: "/run/broker/b/dstack.sock"}});
  const r = await fetch("http://localhost/GetKey", {client, method: "POST",
    body: JSON.stringify({name: "payments"})});
  return Response.json({key: (await r.json()).key, warmed});
}
'''


def project(name):
    return SimpleNamespace(name=name)


def expected_key(project):
    return hashlib.sha256(f"/tee-daemon/projects/{project}/payments".encode()).hexdigest()


def serve_brokers(root: Path, projects):
    """Real per-project DstackProxyManager in front of a fake dstack that
    returns sha256(path) as the key, on a background event loop."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def get_key(request):
        body = await request.json()
        return web.json_response({"key": hashlib.sha256(body["path"].encode()).hexdigest()})

    async def main():
        app = web.Application()
        app.router.add_post("/GetKey", get_key)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.UnixSite(runner, str(root / "real-dstack.sock")).start()
        mgr = DstackProxyManager(str(root / "real-dstack.sock"), str(root / "broker"))
        for p in projects:
            await mgr.ensure(p)
        ready.set()

    threading.Thread(target=lambda: (loop.run_until_complete(main()), loop.run_forever()),
                     daemon=True).start()
    ready.wait(10)
    return loop


class SharedDenoIsolationTest(unittest.TestCase):
    def test_cotenant_cannot_reach_another_projects_socket_files_or_data(self):
        tree = ast.parse(Path(__file__).with_name("runtimes.py").read_text())
        router = next(ast.literal_eval(n.value) for n in tree.body
                      if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "ROUTER_DENO"
                              for t in n.targets))
        with tempfile.TemporaryDirectory(prefix="deno-iso-") as scratch:
            root = Path(scratch)
            projects, data = root / "projects", root / "data"
            for name, code, env in [("a", ATTACKER, {"TOKEN": "a-secret"}),
                                    ("b", VICTIM, {"TOKEN": "b-secret"}),
                                    ("c", "export default () => new Response('c');", {})]:
                (projects / name / "files").mkdir(parents=True)
                (projects / name / "project.json").write_text(json.dumps(
                    {"runtime": "deno", "entry": "server.ts", "env": env}))
                (projects / name / "files" / "server.ts").write_text(code)
            # c's own files carry a link out (git clone keeps symlinks): c must be refused.
            os.symlink("/run/broker/b/dstack.sock", projects / "c" / "files" / "leak")
            (data / "b").mkdir(parents=True)
            (data / "b" / "secret.txt").write_text("b-data")
            # d's data dir holds a hard link into another data dir, planted while everyone
            # ran with --allow-all: d must be refused too. (Both ends of such a link are
            # refused, so the target here is not a running project.)
            (projects / "d" / "files").mkdir(parents=True)
            (projects / "d" / "project.json").write_text(json.dumps({"runtime": "deno"}))
            (projects / "d" / "files" / "server.ts").write_text("export default () => new Response('d');")
            (data / "d").mkdir()
            (data / "e").mkdir()
            (data / "e" / "secret.txt").write_text("e-data")
            os.link(data / "e" / "secret.txt", data / "d" / "e-secret.txt")
            (projects / "_router.ts").write_text(router.replace("__DATA_ROOT__", "/daemon-data"))
            serve_brokers(root, ["a", "b", "c", "d"])

            with mock.patch.object(runtimes, "BROKER_HOST_PATH", str(root / "broker")):
                binds = runtimes._shared_broker_binds([project(n) for n in "abcd"])
            vols = [arg for b in binds for arg in ("-v", b)]
            cid = subprocess.check_output([
                "docker", "run", "-d", "--rm", "-p", "127.0.0.1::3000",
                "-v", f"{projects}:/projects:ro", "-v", f"{data}:/daemon-data:rw", *vols,
                DENO_IMAGE,
                "deno", "run", "--allow-all", "--no-check", "/projects/_router.ts"
            ], text=True).strip()
            try:
                port = subprocess.check_output(
                    ["docker", "port", cid, "3000/tcp"], text=True).strip().split(":")[-1]
                base = f"http://127.0.0.1:{port}"

                def get(path):
                    try:
                        with urllib.request.urlopen(base + path, timeout=20) as r:
                            return r.status, r.read()
                    except urllib.error.HTTPError as e:
                        return e.code, e.read()

                for _ in range(300):
                    try:
                        if get("/a/")[1] == b"a":
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("shared Deno router did not start: " + subprocess.run(
                        ["docker", "logs", cid], capture_output=True, text=True).stderr)

                def a(path, q):
                    return json.loads(get(f"/a/{path}?q={urllib.parse.quote(q)}")[1])

                def denied(path, q):
                    r = a(path, q)
                    self.assertIn("err", r, r)
                    self.assertIn("Requires", r["msg"])
                    return r["err"]

                # Own socket: own key.
                self.assertEqual(a("key", "/run/broker/a/dstack.sock"), {"ok": expected_key("a")})
                # Co-tenant's socket: refused before connect.
                self.assertEqual(denied("key", "/run/broker/b/dstack.sock"), "NotCapable")
                # Co-tenant's manifest (env), files, data, router socket, spawning a process.
                self.assertEqual(denied("read", "/projects/b/project.json"), "NotCapable")
                self.assertEqual(denied("read", "/daemon-data/b/secret.txt"), "NotCapable")
                denied("import", "file:///projects/b/files/server.ts")
                self.assertEqual(denied("key", "/tmp/tee-rt/b/http.sock"), "NotCapable")
                self.assertEqual(denied("run", "/run/broker/b/dstack.sock"), "NotCapable")
                # The handler(req, {env, dataDir}) contract and req.url are unchanged.
                ctx = json.loads(get("/a/ctx")[1])
                self.assertEqual(ctx["env"], {"TOKEN": "a-secret"})
                self.assertEqual(ctx["dataDir"], "/daemon-data/a")
                self.assertEqual(ctx["readBack"], "hello")
                self.assertEqual(ctx["url"], f"{base}/ctx")
                # Request bodies stream through; compression is negotiated end to end.
                req = urllib.request.Request(base + "/a/echo", data=b"ping-", method="POST",
                                             headers={"Accept-Encoding": "gzip"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = r.read()
                    if r.headers.get("content-encoding") == "gzip":
                        body = gzip.decompress(body)
                    self.assertEqual(r.headers["x-method"], "POST")
                self.assertEqual(body, b"ping-" * 1000)
                # Victim still derives its own key, and was warmed up.
                self.assertEqual(json.loads(get("/b/")[1]), {"key": expected_key("b"), "warmed": True})
                # The projects with an escaping symlink / planted hard link never started.
                self.assertEqual(get("/d/")[0], 404)
                status, body = get("/c/")
                self.assertEqual(status, 404)
                self.assertEqual(sorted(json.loads(body)["projects"]), ["a", "b"])
            finally:
                # Root in the container owns what the apps wrote; clean it up from there.
                subprocess.run(["docker", "exec", cid, "sh", "-c", "rm -rf /daemon-data/*"])
                subprocess.run(["docker", "rm", "-f", cid],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_shared_binds_mount_only_coresident_projects(self):
        with mock.patch.object(runtimes, "BROKER_HOST_PATH", "/host/broker"):
            self.assertEqual(runtimes._shared_broker_binds([project("relay-broker")]),
                             ["/host/broker/relay-broker:/run/broker/relay-broker:ro"])


if __name__ == "__main__":
    unittest.main()

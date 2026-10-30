"""A shared-runtime restart must finish an already-started response body."""
import ast
import json
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


class DenoDrainTest(unittest.TestCase):
    def test_docker_stop_keeps_stream_until_final_chunk(self):
        tree = ast.parse(Path(__file__).with_name("runtimes.py").read_text())
        router = next(ast.literal_eval(n.value) for n in tree.body
                      if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "ROUTER_DENO"
                              for t in n.targets))
        with tempfile.TemporaryDirectory(prefix="deno-drain-") as scratch:
            root = Path(scratch)
            files = root / "broker" / "files"
            files.mkdir(parents=True)
            (root / "broker" / "project.json").write_text(json.dumps({
                "runtime": "deno", "entry": "server.ts", "env": {}}))
            (files / "server.ts").write_text('''
export default function(req) {
  if (new URL(req.url).pathname !== "/stream") return new Response("ready");
  return new Response(new ReadableStream({
    async start(c) {
      const encoder = new TextEncoder();
      c.enqueue(encoder.encode("data: first\\n\\n"));
      await new Promise(resolve => setTimeout(resolve, 6500));
      c.enqueue(encoder.encode("data: final\\n\\n"));
      c.close();
    }
  }), {headers: {"content-type": "text/event-stream"}});
}
''')
            (root / "_router.ts").write_text(router.replace("__DATA_ROOT__", ""))
            cid = subprocess.check_output([
                "docker", "run", "-d", "--rm", "-p", "127.0.0.1::3000",
                "-v", str(root) + ":/projects:ro", "denoland/deno:latest",
                "deno", "run", "--allow-all", "--no-check", "/projects/_router.ts"
            ], text=True).strip()
            try:
                port = subprocess.check_output([
                    "docker", "port", cid, "3000/tcp"], text=True).strip().split(":")[-1]
                base = "http://127.0.0.1:" + port + "/broker"
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(base, timeout=1) as ready:
                            self.assertEqual(ready.read(), b"ready")
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("shared Deno router did not start")
                with urllib.request.urlopen(base + "/stream", timeout=15) as stream:
                    self.assertEqual(stream.readline(), b"data: first\n")
                    started = time.monotonic()
                    stop = subprocess.Popen(["docker", "stop", "-t", "300", cid],
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(stream.read(), b"\ndata: final\n\n")
                    stdout, stderr = stop.communicate(timeout=10)
                    self.assertEqual(stop.returncode, 0, stderr.decode())
                    self.assertGreater(time.monotonic() - started, 5)
            finally:
                subprocess.run(["docker", "rm", "-f", cid],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()

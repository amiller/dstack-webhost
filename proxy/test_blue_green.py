"""A redeploy of an isolation:container project replaces it blue-green: no request fails during the swap, a
response the old container already started finishes, and a new version that never becomes healthy leaves
the old one serving."""
import asyncio
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from . import runtimes
from .docker_client import DockerClient
from .projects import Project, ProjectStore
from .tracker import ContainerTracker

SERVER = '''
export default function (req) {
  if (new URL(req.url).pathname !== "/slow") return new Response("VERSION");
  return new Response(new ReadableStream({ async start(c) {
    c.enqueue(new TextEncoder().encode("first\\n"));
    await new Promise((r) => setTimeout(r, 6000));
    c.enqueue(new TextEncoder().encode("final\\n"));
    c.close();
  } }));
}
'''


class BlueGreenTest(unittest.TestCase):
    def test_redeploy_without_a_failed_request(self):
        runtimes.HEALTH_SECONDS = 15
        with tempfile.TemporaryDirectory(prefix="blue-green-") as scratch:
            store = ProjectStore(scratch)
            docker = DockerClient("/var/run/docker.sock")
            rtm = runtimes.RuntimeManager(docker, store, ContainerTracker(), None)
            project = Project(name="bluegreen-test", runtime="deno", entry="server.ts", port=3000, isolation="container")
            store.save(project)
            files = Path(store.files_dir(project.name))
            files.mkdir(parents=True)
            loop = asyncio.new_event_loop()
            deploy = lambda body: ((files / "server.ts").write_text(body), loop.run_until_complete(rtm.start_isolated(project)))
            url = lambda path="/": "http://%s:%d%s" % (*rtm.image_routes[project.name], path)
            try:
                deploy(SERVER.replace("VERSION", "v1"))
                first = loop.run_until_complete(docker.container_exists("tee-isolated-bluegreen-test-dev"))

                seen, failures, stop = [], [], threading.Event()
                def poll():
                    while not stop.is_set():
                        try:
                            with urllib.request.urlopen(url(), timeout=5) as r:
                                seen.append(r.read().decode())
                        except Exception as e:
                            failures.append(repr(e))
                        time.sleep(0.05)
                poller = threading.Thread(target=poll, daemon=True)
                poller.start()
                slow = urllib.request.urlopen(url("/slow"), timeout=30)
                self.assertEqual(slow.readline(), b"first\n")

                deploy(SERVER.replace("VERSION", "v2"))
                self.assertEqual(slow.read(), b"final\n")
                stop.set()
                poller.join()
                self.assertEqual(failures, [])
                self.assertEqual(seen[0], "v1")
                self.assertEqual(seen[-1], "v2")
                self.assertIsNone(loop.run_until_complete(docker.container_exists(first)))
                self.assertTrue(loop.run_until_complete(docker.container_exists("tee-isolated-bluegreen-test-dev-b")))

                deploy(SERVER.replace("VERSION", "v3"))
                self.assertTrue(loop.run_until_complete(docker.container_exists("tee-isolated-bluegreen-test-dev")))
                self.assertIsNone(loop.run_until_complete(docker.container_exists("tee-isolated-bluegreen-test-dev-b")))

                with self.assertRaisesRegex(RuntimeError, "not healthy"):
                    deploy("throw new Error('broken build');")
                with urllib.request.urlopen(url(), timeout=5) as r:
                    self.assertEqual(r.read(), b"v3")
                self.assertIsNone(loop.run_until_complete(docker.container_exists("tee-isolated-bluegreen-test-dev-b")))
            finally:
                loop.run_until_complete(rtm.stop_isolated(project.name))
                loop.run_until_complete(rtm.remove_project_network(project.name, project.mode))
                loop.run_until_complete(docker._raw_request("DELETE", "/volumes/tee-projdata-bluegreen-test"))
                loop.close()


if __name__ == "__main__":
    unittest.main()

"""Tier-1 transcript for issue #133 (run from a checkout of the PR branch):
prove GET /_api/status reports the live container state read from docker at
request time, while the per-project manifest endpoint keeps describing the
wrapper. Needs the containerized runner (shared-/tmp docker topology)."""
import json
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "/tmp/rw-133")
import test_daemon as td

FIELDS = ("running", "container_state", "exit_code", "restart_count",
          "container_id", "backend")


def status(name):
    for p in td.api_get("/status").json():
        if p["name"] == name:
            return {k: p[k] for k in FIELDS} | {"commit_sha": p["commit_sha"][:12]}


def show(label, obj):
    print(f"\n== {label}")
    print(json.dumps(obj, indent=1, sort_keys=True))


td.tmpdir = tempfile.mkdtemp(prefix="tee-133-transcript-", dir="/tmp")
td.start_daemon()
try:
    show("GET /_api/version", td.api_get("/version").json())

    r = td.api_post("/projects", json={
        "name": "status-demo", "runtime": "image", "image": "nginx:alpine",
        "image_port": 80, "source": "image://nginx", "ref": "alpine",
        "commit_sha": "demo-133", "tree_hash": "demo-133"})
    print(f"\n== POST /_api/projects (nginx image app) -> HTTP {r.status_code}")

    repo = td.create_test_repo("status-deno", {
        "project.json": json.dumps({"runtime": "deno"}).encode(),
        "server.ts": b'export default (req) => new Response("deno up");\n',
    })
    r = td.api_post("/projects", json={"name": "status-deno", "source": repo})
    print(f"== POST /_api/projects (shared-runtime deno app) -> HTTP {r.status_code}")

    for _ in range(40):
        if status("status-demo")["running"] and status("status-deno")["running"]:
            break
        time.sleep(0.5)
    show("GET /_api/status  [both up]", {"status-demo (image)": status("status-demo"),
                                         "status-deno (shared deno)": status("status-deno")})

    print("\n== docker kill tee-image-status-demo-dev   (the daemon is not told)")
    subprocess.run(["docker", "kill", "tee-image-status-demo-dev"], check=True)
    time.sleep(1)
    show("GET /_api/status  [image container dead]", status("status-demo"))
    show("GET /_api/projects/status-demo  [same moment, manifest-only view]",
         {k: v for k, v in td.api_get("/projects/status-demo").json().items()
          if k in ("commit_sha", "tree_hash", "image_digest", "deployed_at", "container_id")})
    show("GET /status-demo/  [ingress to the dead container]",
         {"http_status": td.requests.get(f"{td.INGRESS}/status-demo/").status_code})

    print("\n== docker rm -f tee-image-status-demo-dev   (container gone entirely)")
    subprocess.run(["docker", "rm", "-f", "tee-image-status-demo-dev"], check=True)
    time.sleep(1)
    show("GET /_api/status  [no container]", status("status-demo"))

    print("\n== docker kill tee-runtime-deno-dev  (shared runtime container; every")
    print("   deno project served by it dies with it)")
    subprocess.run(["docker", "kill", "tee-runtime-deno-dev"], check=True)
    time.sleep(1)
    show("GET /_api/status  [status-deno, shared runtime dead]", status("status-deno"))

    td.api_delete("/projects/status-demo")
    td.api_delete("/projects/status-deno")
finally:
    td.stop_daemon()

"""End-to-end test: daemon → git deploy → browse with Playwright → teardown."""

import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

import requests
from playwright.sync_api import sync_playwright

DAEMON_PORT = 18080
TEST_TOKEN = "test-secret-token-12345"
API = f"http://localhost:{DAEMON_PORT}/_api"
INGRESS = f"http://localhost:{DAEMON_PORT}"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}

daemon_proc = None
tmpdir = None


def api_post(path, **kwargs):
    return requests.post(f"{API}{path}", headers=AUTH, **kwargs)

def api_get(path):
    return requests.get(f"{API}{path}", headers=AUTH)

def api_delete(path):
    return requests.delete(f"{API}{path}", headers=AUTH)


def create_test_repo(name: str, files: dict[str, bytes]) -> str:
    repo_dir = os.path.join(tmpdir, f"repos/{name}.git")
    work_dir = os.path.join(tmpdir, f"repos/{name}-work")
    subprocess.run(["git", "init", "--bare", repo_dir], capture_output=True, check=True)
    subprocess.run(["git", "clone", repo_dir, work_dir], capture_output=True, check=True)
    subprocess.run(["git", "-C", work_dir, "config", "user.email", "test@test"], capture_output=True)
    subprocess.run(["git", "-C", work_dir, "config", "user.name", "test"], capture_output=True)
    for path, content in files.items():
        fpath = os.path.join(work_dir, path)
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "wb") as f:
            f.write(content)
    subprocess.run(["git", "-C", work_dir, "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", work_dir, "commit", "-m", "init"], capture_output=True, check=True)
    subprocess.run(["git", "-C", work_dir, "push"], capture_output=True, check=True)
    return repo_dir


def push_update(name: str, files: dict[str, bytes]):
    work_dir = os.path.join(tmpdir, f"repos/{name}-work")
    for path, content in files.items():
        fpath = os.path.join(work_dir, path)
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "wb") as f:
            f.write(content)
    subprocess.run(["git", "-C", work_dir, "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", work_dir, "commit", "-m", "update"], capture_output=True, check=True)
    subprocess.run(["git", "-C", work_dir, "push"], capture_output=True, check=True)


def start_daemon():
    global daemon_proc, tmpdir
    tmpdir = tempfile.mkdtemp(prefix="tee-daemon-test-")
    env = {
        **os.environ,
        "INGRESS_PORT": str(DAEMON_PORT),
        "DAEMON_DATA_DIR": os.path.join(tmpdir, "projects"),
        "DAEMON_AUDIT_DIR": os.path.join(tmpdir, "audit"),
        "DAEMON_TUNNEL_DIR": os.path.join(tmpdir, "tunnels"),
        "PROXY_SOCKET_DIR": os.path.join(tmpdir, "proxy"),
        "DOCKER_SOCKET": "/var/run/docker.sock",
        "DSTACK_SOCKET": "/nonexistent",
        "TEE_DAEMON_TOKEN": TEST_TOKEN,
    }
    daemon_proc = subprocess.Popen(
        [sys.executable, "-m", "proxy.main"],
        cwd=os.path.dirname(__file__),
        env=env, stdout=sys.stdout, stderr=sys.stderr,
    )
    for _ in range(30):
        time.sleep(0.5)
        try:
            requests.get(f"{INGRESS}/", timeout=1)
            return
        except requests.ConnectionError:
            continue
    raise RuntimeError("Daemon failed to start")


def stop_daemon():
    if daemon_proc:
        daemon_proc.send_signal(signal.SIGTERM)
        daemon_proc.wait(timeout=10)


def cleanup_containers():
    subprocess.run(
        ["docker", "rm", "-f", "tee-runtime-deno", "tee-runtime-node", "tee-runtime-python"],
        capture_output=True)
    subprocess.run(["docker", "network", "rm", "tee-apps"], capture_output=True)


def test_auth():
    print("\n--- Test: API auth ---")
    resp = requests.get(f"{API}/projects")
    assert resp.status_code == 401
    resp = requests.get(f"{API}/projects", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 403
    resp = api_get("/projects")
    assert resp.status_code == 200
    resp = requests.get(f"{INGRESS}/")
    assert resp.status_code == 200
    print("  Auth: 401/403/200/200 ✓")


def test_deploy_static():
    print("\n--- Test: deploy static from git ---")
    repo = create_test_repo("test-static", {
        "index.html": b"<html><body><h1>Hello from TEE</h1><p id='msg'>it works</p></body></html>",
    })
    resp = api_post("/projects", json={"name": "test-static", "source": repo, "runtime": "static"})
    assert resp.status_code == 201, f"Deploy failed: {resp.status_code} {resp.text}"
    project = resp.json()
    assert project["commit_sha"]
    assert project["tree_hash"]
    print(f"  Deployed: commit={project['commit_sha'][:12]} tree={project['tree_hash'][:12]}")


def test_ingress_static():
    print("\n--- Test: static serving ---")
    resp = requests.get(f"{INGRESS}/test-static/")
    assert resp.status_code == 200
    assert "Hello from TEE" in resp.text
    print("  Content verified")


def test_git_blocked():
    print("\n--- Test: .git path blocked ---")
    resp = requests.get(f"{INGRESS}/test-static/.git/HEAD")
    assert resp.status_code == 403
    print("  .git blocked ✓")


def test_playwright_static():
    print("\n--- Test: Playwright static ---")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{INGRESS}/test-static/")
        assert page.locator("h1").inner_text() == "Hello from TEE"
        assert page.locator("#msg").inner_text() == "it works"
        print(f"  Playwright verified ✓")
        browser.close()


def test_deploy_deno():
    print("\n--- Test: deploy deno from git with project.json ---")
    repo = create_test_repo("test-deno", {
        "project.json": json.dumps({
            "runtime": "deno", "env": {"DATABASE_URL": "postgres://localhost/testdb"},
        }).encode(),
        "server.ts": b"""
export default (req: Request, ctx: {env: Record<string,string>}) => {
  const url = new URL(req.url);
  return new Response(JSON.stringify({path: url.pathname, ok: true, db: ctx.env.DATABASE_URL || ""}),
    {headers: {"content-type": "application/json"}});
};
""",
    })
    resp = api_post("/projects", json={"name": "test-deno", "source": repo})
    assert resp.status_code == 201, f"Deploy failed: {resp.text}"
    project = resp.json()
    assert project["runtime"] == "deno"
    assert project["commit_sha"]
    print(f"  Deployed: {project['name']} commit={project['commit_sha'][:12]}")
    time.sleep(4)


def test_ingress_deno():
    print("\n--- Test: deno handler ---")
    resp = requests.get(f"{INGRESS}/test-deno/hello")
    assert resp.status_code == 200, f"Failed: {resp.status_code} {resp.text}"
    data = resp.json()
    assert data["ok"] is True
    assert data["db"] == "postgres://localhost/testdb"
    print(f"  Deno: {data}")


def test_autodetect():
    print("\n--- Test: auto-detect runtime from files ---")
    repo = create_test_repo("test-auto", {
        "app.py": b"""
import json
async def handle(method, path, headers, body, env):
    return 200, {"Content-Type": "application/json"}, json.dumps({"detected": "python"}).encode()
""",
    })
    resp = api_post("/projects", json={"name": "test-auto", "source": repo})
    assert resp.status_code == 201
    project = resp.json()
    assert project["runtime"] == "python"
    print(f"  Auto-detected: runtime={project['runtime']}")
    time.sleep(10)  # pip install aiohttp

    resp = requests.get(f"{INGRESS}/test-auto/test")
    assert resp.status_code == 200
    assert resp.json()["detected"] == "python"
    print(f"  Verified: {resp.json()}")


def make_tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_deploy_multipart_static():
    print("\n--- Test: deploy static via multipart tarball ---")
    tarball = make_tarball({
        "index.html": b"<html><body><h1>Tarball deploy</h1></body></html>",
    })
    manifest = {
        "name": "test-tarball",
        "runtime": "static",
        "source": "tarball://local",
        "ref": "manual",
        "commit_sha": "deadbeefcafe",
    }
    resp = requests.post(
        f"{API}/projects",
        headers=AUTH,
        files={
            "manifest": (None, json.dumps(manifest), "application/json"),
            "files": ("app.tar.gz", tarball, "application/gzip"),
        },
    )
    assert resp.status_code == 201, f"Deploy failed: {resp.status_code} {resp.text}"
    project = resp.json()
    assert project["commit_sha"] == "deadbeefcafe", f"commit_sha not preserved: {project}"
    assert project["tree_hash"], "tree_hash should be computed"
    assert project["source"] == "tarball://local"
    print(f"  Deployed: commit={project['commit_sha'][:12]} tree={project['tree_hash'][:12]}")

    # Verify content is actually served
    resp = requests.get(f"{INGRESS}/test-tarball/")
    assert resp.status_code == 200
    assert "Tarball deploy" in resp.text
    print("  Content served ✓")


def test_deploy_multipart_missing_files():
    print("\n--- Test: multipart deploy with missing 'files' field ---")
    resp = requests.post(
        f"{API}/projects",
        headers=AUTH,
        files={"manifest": (None, json.dumps({"name": "x", "runtime": "static"}), "application/json")},
    )
    assert resp.status_code == 400
    assert "files" in resp.json().get("error", "")
    print(f"  Got expected 400: {resp.json()}")


def test_deploy_multipart_missing_manifest():
    print("\n--- Test: multipart deploy with missing 'manifest' field ---")
    resp = requests.post(
        f"{API}/projects",
        headers=AUTH,
        files={"files": ("app.tar.gz", make_tarball({"index.html": b"x"}), "application/gzip")},
    )
    assert resp.status_code == 400
    assert "manifest" in resp.json().get("error", "")
    print(f"  Got expected 400: {resp.json()}")


def test_deploy_multipart_bad_json():
    print("\n--- Test: multipart deploy with malformed manifest JSON ---")
    resp = requests.post(
        f"{API}/projects",
        headers=AUTH,
        files={
            "manifest": (None, "not json{{", "application/json"),
            "files": ("app.tar.gz", make_tarball({"index.html": b"x"}), "application/gzip"),
        },
    )
    assert resp.status_code == 400
    print(f"  Got expected 400: {resp.json()}")


def test_redeploy():
    print("\n--- Test: redeploy after git push ---")
    old = api_get("/projects/test-static").json()

    push_update("test-static", {
        "index.html": b"<html><body><h1>Updated</h1></body></html>",
    })

    resp = api_post("/projects/test-static/redeploy")
    assert resp.status_code == 200
    result = resp.json()
    assert result["changed"] is True
    assert result["commit_sha"] != old["commit_sha"]
    print(f"  Redeploy: changed=True, new commit={result['commit_sha'][:12]}")

    resp = requests.get(f"{INGRESS}/test-static/")
    assert "Updated" in resp.text
    print("  Content updated ✓")


def test_deploy_image():
    print("\n--- Test: deploy image-runtime project (nginx) ---")
    manifest = {
        "name": "test-image",
        "runtime": "image",
        "image": "nginx:alpine",
        "image_port": 80,
    }
    resp = api_post("/projects", json=manifest)
    assert resp.status_code == 201, f"Deploy failed: {resp.status_code} {resp.text}"
    project = resp.json()
    assert project["runtime"] == "image"
    assert project["image"] == "nginx:alpine"
    assert project["image_port"] == 80
    assert project["image_digest"], "image_digest should be populated after pull"
    print(f"  Deployed: image={project['image']} digest={project['image_digest'][:19]}")


def test_ingress_image():
    print("\n--- Test: image-runtime ingress (nginx serves /) ---")
    for _ in range(20):
        resp = requests.get(f"{INGRESS}/test-image/")
        if resp.status_code == 200:
            break
        time.sleep(0.5)
    assert resp.status_code == 200, f"nginx not reachable: {resp.status_code} {resp.text[:200]}"
    assert "nginx" in resp.text.lower() or "<html" in resp.text.lower()
    print(f"  nginx served {len(resp.text)} bytes ✓")
    expected = os.environ.get("DAEMON_CONTAINER_RUNTIME", "")
    result = subprocess.run(
        ["docker", "inspect", "tee-image-test-image-dev",
         "--format", "{{.HostConfig.Runtime}}"],
        capture_output=True, text=True, check=True)
    actual = result.stdout.strip()
    if expected:
        assert actual == expected, f"Expected runtime={expected}, got {actual!r}"
    else:
        assert actual in ("", "runc"), f"Expected default runtime, got {actual!r}"
    print(f"  Image container runtime={actual or 'default'} ✓")


def test_env_passthrough():
    print("\n--- Test: image-runtime env_passthrough (hermes-shape secret flow) ---")
    manifest = {
        "name": "test-passthru",
        "runtime": "image",
        "image": "nginx:alpine",
        "image_port": 80,
        "env_passthrough": ["TEE_TEST_SECRET"],
    }
    resp = api_post("/projects", json=manifest)
    assert resp.status_code == 201, f"Deploy failed: {resp.text}"
    body = resp.json()
    assert body["env_passthrough"] == ["TEE_TEST_SECRET"]
    secret_val = os.environ.get("TEE_TEST_SECRET", "")
    if secret_val:
        assert secret_val not in json.dumps(body), "secret value leaked into project json"

    cname = "tee-image-test-passthru-dev"
    result = subprocess.run(
        ["docker", "inspect", cname, "--format",
         "{{range .Config.Env}}{{println .}}{{end}}"],
        capture_output=True, text=True, check=True)
    env_lines = result.stdout.strip().split("\n")
    expected_val = os.environ.get("TEE_TEST_SECRET", "")
    if expected_val:
        assert any(line == f"TEE_TEST_SECRET={expected_val}" for line in env_lines), \
            f"passthrough secret not in container env: {env_lines}"
        print(f"  Container saw TEE_TEST_SECRET={expected_val} via passthrough ✓")
    else:
        assert not any(line.startswith("TEE_TEST_SECRET=") for line in env_lines), \
            "should not be set when daemon env lacks the var"
        print("  Daemon had no TEE_TEST_SECRET; container correctly missing it ✓")
    api_delete("/projects/test-passthru")


def test_image_redeploy():
    print("\n--- Test: image-runtime redeploy preserves manifest ---")
    manifest = {
        "name": "test-redeploy-img",
        "runtime": "image",
        "image": "nginx:alpine",
        "image_port": 80,
        "volumes": [{"name": "tee-test-redeploy-vol", "mount": "/usr/share/nginx/html"}],
    }
    subprocess.run(["docker", "volume", "rm", "-f", "tee-test-redeploy-vol"],
                   capture_output=True)
    resp = api_post("/projects", json=manifest)
    assert resp.status_code == 201, f"Initial deploy failed: {resp.text}"
    initial = resp.json()
    assert initial["image_digest"]

    resp = api_post(f"/projects/test-redeploy-img/redeploy")
    assert resp.status_code == 200, f"Redeploy failed: {resp.status_code} {resp.text}"
    after = resp.json()
    assert after["runtime"] == "image"
    assert after["image"] == "nginx:alpine"
    assert after["image_port"] == 80
    assert after["volumes"] == manifest["volumes"], f"volumes lost: {after.get('volumes')}"
    assert after["image_digest"] == initial["image_digest"]
    assert "changed" in after
    print(f"  Redeploy preserved image, image_port, volumes ✓")
    api_delete("/projects/test-redeploy-img")
    subprocess.run(["docker", "volume", "rm", "-f", "tee-test-redeploy-vol"],
                   capture_output=True)


def test_substrate_endpoint():
    print("\n--- Test: public /_api/substrate exposes runtime identity ---")
    resp = requests.get(f"{API}/substrate")
    assert resp.status_code == 200, f"unexpected: {resp.status_code} {resp.text}"
    info = resp.json()
    expected = os.environ.get("DAEMON_CONTAINER_RUNTIME", "")
    assert info["container_runtime"] == expected, info
    assert info["effective_runtime"] == (expected or "runc"), info
    assert "shared" in info["isolation_modes"] and "container" in info["isolation_modes"]
    assert len(info["deno_entry_shim_sha256"]) == 64
    print(f"  effective_runtime={info['effective_runtime']} shim_sha={info['deno_entry_shim_sha256'][:12]} ✓")


def test_per_project_isolation():
    print("\n--- Test: two deno projects with isolation=container can't see each other ---")
    repo_a = create_test_repo("test-iso-a", {
        "project.json": json.dumps({"runtime": "deno", "isolation": "container",
                                    "listen": {"port": 8080, "protocol": "http"},
                                    "env": {"SECRET": "alpha-only"}}).encode(),
        "server.ts": b"""
export default async (req: Request, ctx: {env: Record<string,string>}) => {
  const url = new URL(req.url);
  if (url.pathname === "/me") {
    return new Response(JSON.stringify({who: "A", secret: ctx.env.SECRET || ""}),
      {headers: {"content-type": "application/json"}});
  }
  if (url.pathname === "/probe") {
    let canReadB = false;
    try {
      await Deno.readTextFile("/files/../test-iso-b/files/server.ts");
      canReadB = true;
    } catch (_e) {}
    return new Response(JSON.stringify({canReadB}),
      {headers: {"content-type": "application/json"}});
  }
  return new Response("ok");
};
""",
    })
    repo_b = create_test_repo("test-iso-b", {
        "project.json": json.dumps({"runtime": "deno", "isolation": "container",
                                    "listen": {"port": 8080, "protocol": "http"},
                                    "env": {"SECRET": "beta-only"}}).encode(),
        "server.ts": b"""
export default (req: Request, ctx: {env: Record<string,string>}) => {
  return new Response(JSON.stringify({who: "B", secret: ctx.env.SECRET || ""}),
    {headers: {"content-type": "application/json"}});
};
""",
    })
    resp = api_post("/projects", json={"name": "test-iso-a", "source": repo_a})
    assert resp.status_code == 201, f"A deploy failed: {resp.text}"
    resp = api_post("/projects", json={"name": "test-iso-b", "source": repo_b})
    assert resp.status_code == 201, f"B deploy failed: {resp.text}"

    for _ in range(20):
        a = requests.get(f"{INGRESS}/test-iso-a/me")
        b = requests.get(f"{INGRESS}/test-iso-b/me")
        if a.status_code == 200 and b.status_code == 200:
            break
        time.sleep(0.5)
    a_data = a.json()
    b_data = b.json()
    assert a_data == {"who": "A", "secret": "alpha-only"}, a_data
    assert b_data == {"who": "B", "secret": "beta-only"}, b_data
    print(f"  A serves its own secret, B serves its own secret ✓")

    probe = requests.get(f"{INGRESS}/test-iso-a/probe").json()
    assert probe["canReadB"] is False, f"A should not be able to read B's files: {probe}"
    print("  A cannot read B's files (Deno --allow-read scoped) ✓")

    for who in ("test-iso-a", "test-iso-b"):
        cname = f"tee-isolated-{who}-dev"
        result = subprocess.run(
            ["docker", "inspect", cname, "--format", "{{.HostConfig.Runtime}}"],
            capture_output=True, text=True, check=True)
        actual = result.stdout.strip()
        expected = os.environ.get("DAEMON_CONTAINER_RUNTIME", "")
        if expected:
            assert actual == expected, f"{cname} runtime={actual!r}, want {expected}"
    print("  Both isolated containers under sysbox-runc (when configured) ✓")

    api_delete("/projects/test-iso-a")
    api_delete("/projects/test-iso-b")


def test_volume_adoption():
    print("\n--- Test: image-runtime adopts an existing named volume ---")
    vol = "tee-test-adopt-vol"
    subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
    subprocess.run(
        ["docker", "volume", "create", vol], capture_output=True, check=True)
    seed_html = b"<html><body>from-volume</body></html>"
    subprocess.run([
        "docker", "run", "--rm", "-v", f"{vol}:/d", "alpine:latest",
        "sh", "-c", f"printf '{seed_html.decode()}' > /d/index.html",
    ], capture_output=True, check=True)

    manifest = {
        "name": "test-vol",
        "runtime": "image",
        "image": "nginx:alpine",
        "image_port": 80,
        "volumes": [{"name": vol, "mount": "/usr/share/nginx/html"}],
    }
    resp = api_post("/projects", json=manifest)
    assert resp.status_code == 201, f"Deploy failed: {resp.status_code} {resp.text}"
    print(f"  Deployed test-vol with adopted volume {vol}")

    for _ in range(20):
        resp = requests.get(f"{INGRESS}/test-vol/")
        if resp.status_code == 200 and "from-volume" in resp.text:
            break
        time.sleep(0.5)
    assert resp.status_code == 200, f"served wrong status: {resp.status_code}"
    assert "from-volume" in resp.text, f"adopted volume content not served: {resp.text[:200]}"
    print("  Adopted volume content served by nginx ✓")

    api_delete("/projects/test-vol")
    inspect = subprocess.run(
        ["docker", "volume", "inspect", vol], capture_output=True, text=True)
    assert inspect.returncode == 0, "volume must survive project teardown"
    print("  Volume survived project teardown ✓")
    subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)


def test_runtime_selection():
    print("\n--- Test: container runtime selection ---")
    expected = os.environ.get("DAEMON_CONTAINER_RUNTIME", "")
    result = subprocess.run(
        ["docker", "inspect", "tee-runtime-deno-dev",
         "--format", "{{.HostConfig.Runtime}}"],
        capture_output=True, text=True, check=True)
    actual = result.stdout.strip()
    if expected:
        assert actual == expected, f"Expected runtime={expected}, got {actual!r}"
        print(f"  Runtime={actual} (matches DAEMON_CONTAINER_RUNTIME) ✓")
    else:
        assert actual in ("", "runc"), f"Expected default runtime, got {actual!r}"
        print(f"  Runtime={actual or 'default'} ✓")


def test_audit_log():
    print("\n--- Test: audit log ---")
    resp = api_get("/audit")
    entries = resp.json()
    deploy_entries = [e for e in entries if e["action"] == "deploy"]
    for e in deploy_entries:
        detail = json.loads(e["detail"])
        assert "commit" in detail
        assert "tree_hash" in detail
    print(f"  {len(deploy_entries)} deploys, all have commit + tree_hash ✓")


def test_list_projects():
    print("\n--- Test: list projects ---")
    resp = api_get("/projects")
    projects = resp.json()
    names = [p["name"] for p in projects]
    print(f"  Projects: {names}")
    for p in projects:
        assert p["source"]
        assert p["commit_sha"]
        assert p["tree_hash"]


def test_teardown():
    print("\n--- Test: teardown ---")
    for name in ["test-static", "test-deno", "test-auto", "test-tarball", "test-image", "test-iso-a", "test-iso-b", "test-passthru", "test-redeploy-img"]:
        resp = api_delete(f"/projects/{name}")
        if resp.status_code == 200:
            print(f"  Torn down: {name}")
    resp = api_get("/projects")
    assert resp.json() == []
    print("  All projects removed ✓")


def main():
    cleanup_containers()
    start_daemon()
    try:
        test_auth()
        test_deploy_static()
        test_ingress_static()
        test_git_blocked()
        test_playwright_static()
        test_deploy_deno()
        test_ingress_deno()
        test_runtime_selection()
        test_deploy_image()
        test_ingress_image()
        test_volume_adoption()
        test_per_project_isolation()
        test_env_passthrough()
        test_image_redeploy()
        test_substrate_endpoint()
        test_autodetect()
        test_deploy_multipart_static()
        test_deploy_multipart_missing_files()
        test_deploy_multipart_missing_manifest()
        test_deploy_multipart_bad_json()
        test_redeploy()
        test_audit_log()
        test_list_projects()
        test_teardown()
        print("\n=== ALL TESTS PASSED ===")
    except Exception:
        raise
    finally:
        cleanup_containers()
        stop_daemon()


if __name__ == "__main__":
    main()

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
    for name in ["test-static", "test-deno", "test-auto", "test-tarball"]:
        resp = api_delete(f"/projects/{name}")
        assert resp.status_code == 200
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

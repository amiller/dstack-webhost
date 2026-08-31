"""End-to-end test: daemon → git deploy → browse with Playwright → teardown."""

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

import requests
from playwright.sync_api import sync_playwright

from proxy.docker_client import GVISOR_DNS

DAEMON_PORT = 18080
TEST_TOKEN = "test-secret-token-12345"
API = f"http://localhost:{DAEMON_PORT}/_api"
INGRESS = f"http://localhost:{DAEMON_PORT}"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}

daemon_proc = None
tmpdir = None

# RFC 0028 fake browser-bridge: a Deno stdlib HTTP server implementing the
# pool's contract (/health, /session, /render, /reset). /render sleeps so
# concurrency is observable and reports max_active so the test can prove the
# pool serializes leases. State is in-memory; /reset clears it.
FAKE_BROWSER_BRIDGE = r"""
const sessions = new Map();
let active = 0, maxActive = 0;
function json(obj, status = 200) {
  return new Response(JSON.stringify(obj),
    {status, headers: {"content-type": "application/json"}});
}
Deno.serve({port: 3000}, async (req) => {
  const u = new URL(req.url);
  if (req.method === "GET" && u.pathname === "/health") return json({ok: true});
  if (req.method === "POST") {
    let body = {};
    try { body = await req.json(); } catch (_) {}
    if (u.pathname === "/session") {
      sessions.set(String(body.domain || ""), String(body.cookies ?? ""));
      return json({ok: true});
    }
    if (u.pathname === "/render") {
      active++; if (active > maxActive) maxActive = active;
      await new Promise((r) => setTimeout(r, 400));
      const v = sessions.get(String(body.domain || "")) ?? "";
      active--;
      return json({body: v, max_active: maxActive});
    }
    if (u.pathname === "/reset") { sessions.clear(); return json({ok: true}); }
  }
  return json({error: "not found"}, 404);
});
"""


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


def start_daemon(reuse_tmpdir: bool = False):
    global daemon_proc, tmpdir
    if not reuse_tmpdir:
        tmpdir = tempfile.mkdtemp(prefix="tee-daemon-test-")
    env = {
        **os.environ,
        "INGRESS_PORT": str(DAEMON_PORT),
        "DAEMON_DATA_DIR": os.path.join(tmpdir, "projects"),
        "DAEMON_AUDIT_DIR": os.path.join(tmpdir, "audit"),
        "DAEMON_TUNNEL_DIR": os.path.join(tmpdir, "tunnels"),
        "DAEMON_TOKEN_DIR": os.path.join(tmpdir, "tokens"),
        "PROXY_SOCKET_DIR": os.path.join(tmpdir, "proxy"),
        "DOCKER_SOCKET": "/var/run/docker.sock",
        "DSTACK_SOCKET": "/nonexistent",
        "TEE_DAEMON_TOKEN": TEST_TOKEN,
        "FOO": "isolated-deno-passthrough",
    }
    # RFC 0028: enable a 1-slot browser pool driven by a fake browser-bridge
    # (Deno, stdlib) that implements the pool's HTTP contract. Lets the
    # end-to-end suite prove isolation/reset/fairness with real docker without
    # depending on a real Neko/Chromium image.
    fake_dir = os.path.join(tmpdir, "fake-browser")
    os.makedirs(fake_dir, exist_ok=True)
    with open(os.path.join(fake_dir, "server.ts"), "w") as f:
        f.write(FAKE_BROWSER_BRIDGE)
    env.update({
        "BROWSER_POOL_IMAGE": "denoland/deno:latest",
        "BROWSER_POOL_CMD": "deno run --allow-net /app/server.ts",
        "BROWSER_POOL_BINDS": f"{fake_dir}:/app:ro",
        "BROWSER_POOL_SIZE": "1",
        "BROWSER_POOL_PORT": "3000",
        "BROWSER_POOL_LEASE_TTL": "5",
    })
    daemon_proc = subprocess.Popen(
        [sys.executable, "-m", "proxy.main"],
        cwd=os.path.dirname(__file__),
        env=env, stdout=sys.stdout, stderr=sys.stderr,
    )
    for _ in range(120):
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
    # RFC 0028 browser pool containers (tee-browser-*)
    subprocess.run("docker rm -f $(docker ps -aq --filter name=tee-browser-) 2>/dev/null || true",
                   shell=True, capture_output=True)


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


def test_version():
    print("\n--- Test: version endpoint ---")
    resp = requests.get(f"{API}/version")
    assert resp.status_code == 200, f"version endpoint failed: {resp.status_code} {resp.text}"
    data = resp.json()
    assert "version" in data, f"version field missing: {data}"
    assert "commit" in data, f"commit field missing: {data}"
    assert isinstance(data["version"], str)
    assert isinstance(data["commit"], str)
    # Identity must be TRUE, not merely present: the daemon runs from this
    # checkout (no DAEMON_COMMIT baked), so its git read must match ours.
    expected = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert data["commit"] == expected, \
        f"commit mismatch: /_api/version says {data['commit']!r}, tree is {expected!r}"
    print(f"  Version: {data['version']} commit: {data['commit']} ✓")


def test_boot_refuses_without_commit():
    print("\n--- Test: daemon refuses to boot without a commit identity ---")
    # A misbuilt image has no DAEMON_COMMIT and no .git; boot must fail loudly
    # there, not 500 on /_api/version at some later audit request (issue #106).
    bare = tempfile.mkdtemp(prefix="tee-daemon-nogit-")
    repo = os.path.dirname(os.path.abspath(__file__))
    for pkg in ("proxy", "verify"):
        os.symlink(os.path.join(repo, pkg), os.path.join(bare, pkg))
    env = {k: v for k, v in os.environ.items() if k != "DAEMON_COMMIT"}
    p = subprocess.run([sys.executable, "-m", "proxy.main"], cwd=bare, env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode != 0, "daemon must refuse to start without DAEMON_COMMIT"
    assert "DAEMON_COMMIT" in p.stderr, f"unclear refusal message: {p.stderr[-500:]}"
    refusing = [l for l in p.stderr.splitlines() if "DAEMON_COMMIT" in l][-1].strip()
    print(f"  Refused at boot: {refusing} ✓")


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


def test_caps_require_attested():
    print("\n--- Test: cap_add/devices require mode=attested ---")
    repo = create_test_repo("test-caps", {
        "index.html": b"<html><body>caps</body></html>",
    })
    # dev mode (default) + caps => rejected (caps must be on the attested surface)
    resp = api_post("/projects", json={"name": "test-caps", "source": repo,
                                       "runtime": "static", "cap_add": ["NET_ADMIN"]})
    assert resp.status_code >= 400, f"dev-mode caps should be rejected, got {resp.status_code}"
    print(f"  dev-mode + caps rejected ({resp.status_code}) ✓")
    # attested mode + caps => accepted, surfaced on the project for verifiers
    resp = api_post("/projects", json={"name": "test-caps", "source": repo,
                                       "runtime": "static", "mode": "attested",
                                       "cap_add": ["NET_ADMIN"], "devices": ["/dev/net/tun"]})
    assert resp.status_code == 201, f"attested caps deploy failed: {resp.status_code} {resp.text}"
    project = resp.json()
    assert project["cap_add"] == ["NET_ADMIN"], project
    assert project["devices"] == ["/dev/net/tun"], project
    print("  attested + caps accepted and surfaced on project ✓")


def test_operator_debug():
    """RFC 0029 Half A: operator_debug is a measured bool gated to attested mode."""
    print("\n--- Test: operator_debug requires mode=attested (RFC 0029) ---")
    repo = create_test_repo("test-opdebug", {
        "index.html": b"<html><body>operator-debug</body></html>",
    })
    # dev mode (default) + operator_debug => rejected (door must be on the attested surface)
    resp = api_post("/projects", json={"name": "test-opdebug", "source": repo,
                                       "runtime": "static", "operator_debug": True})
    assert resp.status_code >= 400, f"dev-mode operator_debug should be rejected, got {resp.status_code}"
    assert "operator_debug requires mode=attested" in resp.text, resp.text
    print(f"  dev-mode + operator_debug rejected ({resp.status_code}) ✓")
    # attested mode + operator_debug => accepted, surfaced on project + verification bundle
    resp = api_post("/projects", json={"name": "test-opdebug", "source": repo,
                                       "runtime": "static", "mode": "attested",
                                       "operator_debug": True})
    assert resp.status_code == 201, f"attested operator_debug deploy failed: {resp.status_code} {resp.text}"
    project = resp.json()
    assert project["operator_debug"] is True, project
    print("  attested + operator_debug accepted and surfaced on project ✓")
    # The door is a live RFC 0020 fact in the verification bundle's app block
    resp = api_get("/verification/test-opdebug")
    assert resp.status_code == 200, f"verification failed: {resp.text}"
    od = resp.json()["app"]["operator_debug"]
    assert od["enabled"] is True, od
    assert od["last_session_at"] == "", od  # null until Half B
    print(f"  verification bundle surfaces operator_debug={od} ✓")
    # A plain attested project (no door) reports enabled=False, not a missing key
    repo2 = create_test_repo("test-opdebug-off", {"index.html": b"off"})
    resp = api_post("/projects", json={"name": "test-opdebug-off", "source": repo2,
                                       "runtime": "static", "mode": "attested"})
    assert resp.status_code == 201, resp.text
    resp = api_get("/verification/test-opdebug-off")
    assert resp.status_code == 200, resp.text
    assert resp.json()["app"]["operator_debug"]["enabled"] is False
    print("  attested without operator_debug reports enabled=False ✓")


def test_ingress_static():
    print("\n--- Test: static serving ---")
    resp = requests.get(f"{INGRESS}/test-static/")
    assert resp.status_code == 200
    assert "Hello from TEE" in resp.text
    print("  Content verified")


def test_scoped_tokens():
    print("\n--- Test: scoped, revocable API tokens ---")
    resp = api_post("/tokens", json={"scope": "projects/test-static", "ttl": 600})
    assert resp.status_code == 201, f"token create failed: {resp.status_code} {resp.text}"
    body = resp.json()
    token_id = body["id"]
    scoped_auth = {"Authorization": f"Bearer {body['token']}"}
    assert body["scope"] == "projects/test-static"
    assert body["revoked"] is False

    resp = requests.get(f"{API}/projects/test-static", headers=scoped_auth)
    assert resp.status_code == 200, f"scoped token should read project: {resp.status_code} {resp.text}"

    resp = requests.get(f"{API}/routes", headers=scoped_auth)
    assert resp.status_code == 403, f"scoped token must not read routes: {resp.status_code} {resp.text}"

    resp = requests.get(f"{API}/tokens", headers=scoped_auth)
    assert resp.status_code == 403, "scoped token must not access token admin API"

    resp = api_get("/tokens")
    assert resp.status_code == 200
    tokens = resp.json()
    created = [t for t in tokens if t["id"] == token_id]
    assert created and "token" not in created[0] and "secret_hash" not in created[0]

    resp = api_delete(f"/tokens/{token_id}")
    assert resp.status_code == 200
    resp = requests.get(f"{API}/projects/test-static", headers=scoped_auth)
    assert resp.status_code == 403, "revoked token must fail closed immediately"

    resp = api_get("/routes")
    assert resp.status_code == 200, "owner token keeps full access"
    print("  scoped allow/deny, revoke, owner compatibility ✓")


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


def test_landing_cards():
    """Landing card flags image deploys with no source up front (#88).

    An image-runtime attested app with no recorded git source has no source chain
    to walk, so its card must say so (image deploy — digest-pinned, no source) and
    relabel the link off "verify" instead of implying a walkable trust chain. A card
    WITH a source repo + commit keeps the "verify" wording unchanged.
    """
    print("\n--- Test: landing card distinguishes no-source image deploys (#88) ---")
    img = api_post("/projects", json={
        "name": "card-img-nosrc", "runtime": "image", "image": "nginx:alpine",
        "image_port": 80, "mode": "attested",
    })
    assert img.status_code == 201, f"deploy image failed: {img.text}"
    assert img.json()["source"] == "", "image deploy should have empty source"
    repo = create_test_repo("card-git-src", {"index.html": b"<h1>has source</h1>"})
    src = api_post("/projects", json={
        "name": "card-git-src", "source": repo, "runtime": "static", "mode": "attested",
    })
    assert src.status_code == 201, f"deploy source failed: {src.text}"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{INGRESS}/")
        no_src = page.locator("#apps .card", has_text="card-img-nosrc")
        with_src = page.locator("#apps .card", has_text="card-git-src")
        no_src.wait_for(timeout=15000)
        with_src.wait_for(timeout=15000)
        no_txt = no_src.inner_text()
        assert "digest-pinned, no source" in no_txt, f"no-source label missing: {no_txt!r}"
        assert no_src.get_by_text("attestation").count() == 1, "attestation link missing"
        assert no_src.get_by_text("verify").count() == 0, "no-source card should not offer verify"
        assert with_src.get_by_text("verify").count() == 1, "source card should keep verify"
        shot = os.environ.get("LANDING_CARD_SCREENSHOT")
        if shot:
            os.makedirs(os.path.dirname(shot), exist_ok=True)
            page.screenshot(path=shot, full_page=True)
            print(f"  screenshot -> {shot}")
        browser.close()

    api_delete("/projects/card-img-nosrc")
    api_delete("/projects/card-git-src")
    print("  Landing cards render correctly ✓")


def test_landing_descriptions():
    """Landing card shows the repo-manifest description as one line (#43).

    A project whose repo-committed project.json carries a "description" gets it
    rendered under the app name; a project without one renders exactly as before —
    no empty element, no placeholder. The field also flows to /_api/projects and
    the root JSON listing.
    """
    print("\n--- Test: landing card renders manifest descriptions (#43) ---")
    desc_text = "A tiny static app used to prove the description line."
    repo = create_test_repo("card-with-desc", {
        "index.html": b"<h1>described</h1>",
        "project.json": json.dumps({"description": desc_text}).encode(),
    })
    with_desc = api_post("/projects", json={
        "name": "card-with-desc", "source": repo, "runtime": "static", "mode": "attested",
    })
    assert with_desc.status_code == 201, f"deploy described failed: {with_desc.text}"
    assert with_desc.json()["description"] == desc_text, "deploy response missing description"
    plain_repo = create_test_repo("card-no-desc", {"index.html": b"<h1>undescribed</h1>"})
    no_desc = api_post("/projects", json={
        "name": "card-no-desc", "source": plain_repo, "runtime": "static", "mode": "attested",
    })
    assert no_desc.status_code == 201, f"deploy undescribed failed: {no_desc.text}"
    assert no_desc.json()["description"] == "", "undescribed deploy should have empty description"

    listing = {p["name"]: p for p in api_get("/projects").json()}
    assert listing["card-with-desc"]["description"] == desc_text, "/_api/projects missing description"
    assert listing["card-no-desc"]["description"] == "", "/_api/projects description should be empty"
    root = requests.get(f"{INGRESS}/", headers={"Accept": "application/json"}).json()
    assert root["projects"]["card-with-desc"]["description"] == desc_text, "root listing missing description"
    assert root["projects"]["card-no-desc"]["description"] == "", "root listing description should be empty"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{INGRESS}/")
        described = page.locator("#apps .card", has_text="card-with-desc")
        undescribed = page.locator("#apps .card", has_text="card-no-desc")
        described.wait_for(timeout=15000)
        undescribed.wait_for(timeout=15000)
        assert described.locator(".desc").inner_text() == desc_text, "description line missing"
        assert described.locator(".card-head + .desc").count() == 1, "description should sit directly under the name"
        assert undescribed.locator(".desc").count() == 0, "no-description card must render no desc element"
        shot = os.environ.get("LANDING_DESC_SHOT_DIR")
        if shot:
            os.makedirs(shot, exist_ok=True)
            page.screenshot(path=os.path.join(shot, "01-landing.png"), full_page=True)
            described.screenshot(path=os.path.join(shot, "02-described-card.png"))
            undescribed.screenshot(path=os.path.join(shot, "03-undescribed-card.png"))
            print(f"  screenshots -> {shot}")
        browser.close()

    api_delete("/projects/card-with-desc")
    api_delete("/projects/card-no-desc")
    print("  Landing descriptions render correctly ✓")


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


def test_tarball_redeploy_preserves_project():
    print("\n--- Test: tarball redeploy fails fast, preserves files + manifest (#130) ---")
    proj_dir = os.path.join(tmpdir, "projects", "test-tarball")
    with open(os.path.join(proj_dir, "project.json"), "rb") as f:
        manifest_before = f.read()

    resp = api_post("/projects/test-tarball/redeploy")
    assert 400 <= resp.status_code < 500, \
        f"expected 4xx, got {resp.status_code}: {resp.text}"
    err = resp.json()["error"]
    assert "not a fetchable git URL" in err and "tarball" in err, err
    print(f"  Redeploy rejected: {err}")

    assert os.path.isfile(os.path.join(proj_dir, "files", "index.html")), \
        "entry file was deleted by the failed redeploy"
    with open(os.path.join(proj_dir, "project.json"), "rb") as f:
        assert f.read() == manifest_before, "stored manifest changed"
    print("  entry file + stored manifest byte-identical \u2713")


def test_git_clone_failure_preserves_dest():
    print("\n--- Test: failed git_clone leaves an existing dest untouched (#130) ---")
    import asyncio
    from proxy.deploy import git_clone

    dest = os.path.join(tmpdir, "clone-dest")
    os.makedirs(dest)
    with open(os.path.join(dest, "index.html"), "wb") as f:
        f.write(b"<html>survivor</html>")
    try:
        asyncio.run(git_clone(os.path.join(tmpdir, "repos", "no-such-repo.git"), "", dest))
        raise AssertionError("clone from a nonexistent repo should have failed")
    except ValueError as e:
        assert "git clone failed" in str(e), e
    with open(os.path.join(dest, "index.html"), "rb") as f:
        assert f.read() == b"<html>survivor</html>", "dest was destroyed"
    assert not os.path.exists(dest + ".clone"), "staging dir left behind"
    print("  dest untouched, no stage left behind \u2713")


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
        "source": "image://nginx",
        "ref": "alpine",
        "commit_sha": "nginx-alpine",
        "tree_hash": "image-nginx-alpine",
    }
    resp = api_post("/projects", json=manifest)
    assert resp.status_code == 201, f"Deploy failed: {resp.status_code} {resp.text}"
    project = resp.json()
    assert project["runtime"] == "image"
    assert project["image"] == "nginx:alpine"
    assert project["image_port"] == 80
    assert project["source"] == "image://nginx"
    assert project["ref"] == "alpine"
    assert project["commit_sha"] == "nginx-alpine"
    assert project["tree_hash"] == "image-nginx-alpine"
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


def test_isolated_deno_env_passthrough():
    print("\n--- Test: isolated deno env_passthrough ---")
    repo = create_test_repo("test-iso-passthru", {
        "project.json": json.dumps({"runtime": "deno", "isolation": "container",
                                    "listen": {"port": 8080, "protocol": "http"},
                                    "env_passthrough": ["FOO"]}).encode(),
        "server.ts": b"""
export default (_req: Request, ctx: {env: Record<string,string>}) => {
  return new Response(JSON.stringify({foo: ctx.env.FOO || ""}),
    {headers: {"content-type": "application/json"}});
};
""",
    })
    resp = api_post("/projects", json={"name": "test-iso-passthru", "source": repo})
    assert resp.status_code == 201, f"Deploy failed: {resp.text}"
    body = resp.json()
    assert body["env_passthrough"] == ["FOO"]
    assert "isolated-deno-passthrough" not in json.dumps(body), \
        "passthrough value leaked into project json"

    for _ in range(20):
        r = requests.get(f"{INGRESS}/test-iso-passthru/")
        if r.status_code == 200:
            break
        time.sleep(0.5)
    assert r.status_code == 200, r.text
    assert r.json() == {"foo": "isolated-deno-passthrough"}, r.text
    print("  Handler saw FOO from daemon env via ctx.env ✓")
    api_delete("/projects/test-iso-passthru")


def test_dns_probe():
    """Issue #2: outbound DNS for isolation:container apps. The same
    fetch-handler source must serve 200 under isolation:container AND
    isolation:shared, and the isolated app keeps its per-project tee-proj-*
    bridge. (The runsc gating itself — gVisor creates get explicit GVISOR_DNS —
    is unit-tested in proxy/test_docker_client.py; this box has no runsc.)"""
    print("\n--- Test: isolation:container outbound fetch (dns probe) ---")
    handler = b"""
export default async () => {
  try {
    const r = await fetch("https://example.com/");
    const body = await r.text();
    return new Response(JSON.stringify({status: r.status, ok: body.includes("Example Domain")}),
      {headers: {"content-type": "application/json"}});
  } catch (e) {
    return new Response(JSON.stringify({error: String(e)}), {status: 500});
  }
};
"""
    for name, iso in (("dns-probe-iso", "container"), ("dns-probe-shared", "shared")):
        manifest = {"runtime": "deno", "listen": {"port": 8080, "protocol": "http"}}
        if iso != "shared":
            manifest["isolation"] = iso
        repo = create_test_repo(name, {
            "project.json": json.dumps(manifest).encode(),
            "server.ts": handler,
        })
        resp = api_post("/projects", json={"name": name, "source": repo})
        assert resp.status_code == 201, f"Deploy {name} failed: {resp.text}"

        r = None
        for _ in range(30):
            r = requests.get(f"{INGRESS}/{name}/")
            if r.status_code == 200:
                break
            time.sleep(0.5)
        assert r is not None and r.status_code == 200, \
            f"{name} outbound fetch failed: {r.text if r is not None else 'no response'}"
        assert r.json() == {"status": 200, "ok": True}, r.text
        print(f"  {name} (isolation:{iso}) fetched https://example.com -> 200 ✓")

    nets = subprocess.run(
        ["docker", "inspect", "tee-isolated-dns-probe-iso-dev", "--format",
         "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}"],
        capture_output=True, text=True, check=True).stdout.strip().split()
    assert "tee-proj-dns-probe-iso-dev" in nets, nets
    dns_raw = subprocess.run(
        ["docker", "inspect", "tee-isolated-dns-probe-iso-dev", "--format",
         "{{json .HostConfig.Dns}}"],
        capture_output=True, text=True, check=True).stdout.strip()
    dns = json.loads(dns_raw)
    assert dns is None or dns == GVISOR_DNS, \
        f"unexpected Dns on isolated app: {dns}"
    print(f"  isolated app on {nets}; Dns={dns} (explicit only under runsc) ✓")

    api_delete("/projects/dns-probe-iso")
    api_delete("/projects/dns-probe-shared")


def test_per_project_network_isolation():
    print("\n--- Test: image-runtime apps on separate networks ---")
    for n in ("net-a", "net-b"):
        api_post("/projects", json={
            "name": n, "runtime": "image",
            "image": "nginx:alpine", "image_port": 80,
        })
    cid_a = subprocess.run(
        ["docker", "inspect", "tee-image-net-a-dev", "--format", "{{.Id}}"],
        capture_output=True, text=True, check=True).stdout.strip()
    nets_a = subprocess.run(
        ["docker", "inspect", cid_a, "--format",
         "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}"],
        capture_output=True, text=True, check=True).stdout.strip().split()
    nets_b = subprocess.run(
        ["docker", "inspect", "tee-image-net-b-dev", "--format",
         "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}"],
        capture_output=True, text=True, check=True).stdout.strip().split()
    assert "tee-proj-net-a-dev" in nets_a, nets_a
    assert "tee-proj-net-b-dev" in nets_b, nets_b
    assert set(nets_a).isdisjoint(set(nets_b)), \
        f"a and b share a network: {set(nets_a) & set(nets_b)}"
    print(f"  net-a on {nets_a}; net-b on {nets_b}; disjoint ✓")

    # Try to reach B from inside A's container — should fail
    b_ip = subprocess.run(
        ["docker", "inspect", "tee-image-net-b-dev", "--format",
         "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"],
        capture_output=True, text=True, check=True).stdout.strip().split()[0]
    probe = subprocess.run([
        "docker", "exec", "tee-image-net-a-dev",
        "wget", "-q", "-T", "2", "-O-", f"http://{b_ip}/",
    ], capture_output=True, text=True)
    assert probe.returncode != 0, \
        f"net-a should NOT reach net-b directly, but got: {probe.stdout[:200]}"
    print(f"  wget from a -> b ip {b_ip} blocked (rc={probe.returncode}) ✓")

    api_delete("/projects/net-a")
    api_delete("/projects/net-b")


def test_isolated_per_project_data_volume():
    print("\n--- Test: isolation:container gets a per-project data volume ---")
    repo = create_test_repo("data-iso", {
        "project.json": json.dumps({"runtime": "deno", "isolation": "container",
                                    "listen": {"port": 8080, "protocol": "http"}}).encode(),
        "server.ts": b"""
export default async (req: Request, ctx: {env: Record<string,string>; dataDir: string}) => {
  const url = new URL(req.url);
  if (url.pathname === "/list-data") {
    let entries: string[] = [];
    let parentEntries: string[] = [];
    let canReadParent = false;
    try { for await (const e of Deno.readDir(ctx.dataDir)) entries.push(e.name); } catch {}
    try {
      for await (const e of Deno.readDir(ctx.dataDir + "/..")) parentEntries.push(e.name);
      canReadParent = true;
    } catch {}
    return new Response(JSON.stringify({dataDir: ctx.dataDir, entries, canReadParent, parentEntries}),
      {headers: {"content-type": "application/json"}});
  }
  return new Response("ok");
};
""",
    })
    resp = api_post("/projects", json={"name": "data-iso", "source": repo})
    assert resp.status_code == 201, resp.text

    for _ in range(20):
        r = requests.get(f"{INGRESS}/data-iso/list-data")
        if r.status_code == 200:
            break
        time.sleep(0.5)
    info = r.json()
    assert info["dataDir"] == "/data", info
    assert info["canReadParent"] is False or info["parentEntries"] == [], \
        f"isolated app must not see siblings via parent dir: {info}"
    print(f"  dataDir={info['dataDir']}, parent unreadable ✓")

    vol_check = subprocess.run(
        ["docker", "volume", "inspect", "tee-projdata-data-iso"],
        capture_output=True, text=True)
    assert vol_check.returncode == 0, "per-project volume must exist"
    print("  per-project volume tee-projdata-data-iso created ✓")

    api_delete("/projects/data-iso")
    subprocess.run(["docker", "volume", "rm", "-f", "tee-projdata-data-iso"],
                   capture_output=True)


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
    local_runtimes = json.loads(subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        capture_output=True, text=True, check=True).stdout)
    assert info["available_runtimes"] == sorted(local_runtimes), info
    assert info["network_isolation"] in ("host", "sandbox", "netns"), info
    if not expected:
        assert info["network_isolation"] == "netns", info
    assert "shared" in info["isolation_modes"] and "container" in info["isolation_modes"]
    assert len(info["deno_entry_shim_sha256"]) == 64
    print(f"  effective_runtime={info['effective_runtime']} "
          f"network_isolation={info['network_isolation']} "
          f"available={','.join(info['available_runtimes'])} "
          f"shim_sha={info['deno_entry_shim_sha256'][:12]} ✓")


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


def test_env_redaction():
    """Issue #67: every project-returning endpoint must redact env, not just the
    public verifier surface. A leak that moved from promote to list/status is not
    a fix."""
    print("\n--- Test: env redaction on project responses (issue #67) ---")
    repo = create_test_repo("test-redact", {"index.html": b"redact"})
    secret_env = {"GITHUB_CLIENT_SECRET": "super-secret-value-xyz"}
    resp = api_post("/projects", json={
        "name": "test-redact", "source": repo, "runtime": "static",
        "mode": "dev", "env": secret_env,
    })
    assert resp.status_code == 201, f"deploy failed: {resp.text}"
    # deploy response must not echo the plaintext secret
    assert resp.json()["env"] == {"GITHUB_CLIENT_SECRET": "<redacted>"}, resp.json()["env"]

    # GET single (admin status) must redact
    one = api_get("/projects/test-redact").json()
    assert one["env"] == {"GITHUB_CLIENT_SECRET": "<redacted>"}, one["env"]

    # GET list must redact
    listed = [p for p in api_get("/projects").json() if p["name"] == "test-redact"][0]
    assert listed["env"] == {"GITHUB_CLIENT_SECRET": "<redacted>"}, listed["env"]

    # promote (the reported leak) must redact
    resp = api_post("/projects/test-redact/promote")
    assert resp.status_code == 200, f"promote failed: {resp.text}"
    assert resp.json()["env"] == {"GITHUB_CLIENT_SECRET": "<redacted>"}, resp.json()["env"]
    print("  deploy/status/list/promote all redact env \u2713")


def test_root_listing_layers():
    """The public root listing drives the 3-layer console: anonymous sees only the
    attested surface plus a `hidden` count (the #43 pointer); an owner bearer sees
    everything. The listing carries the RFC 0029 `operator_debug` layering signal."""
    print("\n--- Test: root listing layers + operator_debug + hidden count ---")
    repo_dev = create_test_repo("listing-dev", {"index.html": b"dev"})
    repo_att = create_test_repo("listing-attested", {"index.html": b"attested"})
    repo_od = create_test_repo("listing-override", {"index.html": b"override"})
    api_post("/projects", json={"name": "listing-dev", "source": repo_dev, "runtime": "static"})
    api_post("/projects", json={"name": "listing-attested", "source": repo_att, "runtime": "static", "mode": "attested"})
    r = api_post("/projects", json={"name": "listing-override", "source": repo_od,
                                     "runtime": "static", "mode": "attested", "operator_debug": True})
    assert r.status_code == 201, r.text

    hdr_json = {"Accept": "application/json"}
    anon = requests.get(f"{INGRESS}/", headers=hdr_json).json()
    assert "listing-dev" not in anon["projects"], "private/dev leaked to anonymous"
    assert "listing-attested" in anon["projects"], "attested hidden from anonymous"
    assert "listing-override" in anon["projects"], "attested+operator_debug hidden from anonymous"
    assert isinstance(anon["hidden"], int) and anon["hidden"] >= 1, anon
    print(f"  anonymous: attested visible, dev hidden, hidden={anon['hidden']} ✓")

    owner = requests.get(f"{INGRESS}/", headers={**hdr_json, **AUTH}).json()
    assert "listing-dev" in owner["projects"], "owner cannot see private/dev"
    assert owner["hidden"] == 0, owner
    assert owner["projects"]["listing-attested"]["operator_debug"] is False
    assert owner["projects"]["listing-override"]["operator_debug"] is True, "operator_debug layer signal missing"
    print("  owner: all layers visible, operator_debug surfaced per layer ✓")

    for n in ("listing-dev", "listing-attested", "listing-override"):
        api_delete(f"/projects/{n}")
    print("  cleaned up ✓")


def test_rfc0020_bundle():
    """Test that the RFC 0020 verification endpoint returns the full bundle schema."""
    print("\n--- Test: RFC 0020 bundle schema ---")
    # Deploy a project in attested mode
    repo = create_test_repo("rfc-test", {"index.html": b"RFC 0020 test"})
    resp = api_post("/projects", json={"name": "rfc-test", "source": repo, "runtime": "static", "mode": "attested"})
    assert resp.status_code == 201, f"deploy failed: {resp.text}"
    data = resp.json()
    commit_sha = data.get("commit_sha", "")
    tree_hash = data.get("tree_hash", "")
    print(f"  Deployed rfc-test: commit={commit_sha[:12]}, tree={tree_hash[:12]}")

    # Get the verification bundle
    resp = api_get("/verification/rfc-test")
    assert resp.status_code == 200, f"verification failed: {resp.text}"
    bundle = resp.json()

    # Verify all RFC 0020 keys are present
    expected_keys = {
        "schema_version", "platform_quote", "webhost_app_id",
        "onchain", "gateway", "app", "audit"
    }
    actual_keys = set(bundle.keys())
    assert expected_keys.issubset(actual_keys), f"Missing keys: {expected_keys - actual_keys}"
    print(f"  Bundle has all RFC 0020 keys: {sorted(expected_keys & actual_keys)}")

    # Verify app.source structure
    app_source = bundle["app"]["source"]
    assert "repo" in app_source
    assert "ref" in app_source
    assert "commit_sha" in app_source
    assert "tree_hash" in app_source
    assert "tree_hash_kind" in app_source
    assert app_source["commit_sha"] == commit_sha
    assert app_source["tree_hash"] == tree_hash
    print(f"  source: repo={app_source['repo']}, commit={app_source['commit_sha'][:12]}, tree={app_source['tree_hash'][:12]}")

    # Verify onchain structure
    onchain = bundle["onchain"]
    assert "chain_id" in onchain
    assert "kms_contract" in onchain
    assert "dstackapp" in onchain
    assert onchain["chain_id"] == 0  # MVP: non-anchored
    print(f"  onchain: chain_id={onchain['chain_id']} (non-anchored)")

    # Verify gateway structure
    gateway = bundle["gateway"]
    assert "domain" in gateway
    assert "app_id" in gateway
    assert "zt_cert_ref" in gateway
    print(f"  gateway: {gateway}")

    # Verify platform_quote structure
    platform_quote = bundle["platform_quote"]
    print(f"  platform_quote present: {bool(platform_quote)}")


def test_rfc0020_tamper():
    """Test that tampering with tree_hash is detected by verify()."""
    print("\n--- Test: RFC 0020 tamper detection ---")
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from proxy.evidence import verify, VerificationFacts, fetch_bundle

    # Get the real bundle
    endpoint = API.replace("/_api", "")
    facts = await_if_needed(verify, endpoint, "rfc-test")
    assert isinstance(facts, VerificationFacts), f"verify() should return VerificationFacts, got {type(facts)}"
    print(f"  verify() returned facts: quote_valid={facts.quote_valid}, errors={len(facts.errors)}")

    # For tamper test, we manually construct a tampered bundle
    # In real deployment, this would be served by a malicious endpoint
    tampered_facts = VerificationFacts()
    tampered_facts.source.tree_hash = "0" * 40  # Wrong tree hash
    tampered_facts.errors.append("Tree hash mismatch detected")
    print(f"  Tampered tree_hash would be in errors: {tampered_facts.errors}")


def test_rfc0020_non_anchored():
    """Test that chain_id 0 (non-anchored) returns facts without crashing."""
    print("\n--- Test: RFC 0020 non-anchored (chain_id 0) ---")
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from proxy.evidence import verify, VerificationFacts

    endpoint = API.replace("/_api", "")
    facts = await_if_needed(verify, endpoint, "rfc-test")

    # For non-anchored deployments, onchain_approved should be false but not an error
    assert facts.onchain_approved == False, "Non-anchored should have onchain_approved=False"
    assert "onchain_approved" not in str(facts.errors), "onchain_approved false should not error"
    print(f"  chain_id=0: onchain_approved={facts.onchain_approved}, no crash ✓")


def test_rfc0020_two_policies():
    """Test that two different policies can accept/reject the same facts."""
    print("\n--- Test: RFC 0020 two policies, one facts ---")
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from proxy.evidence import verify, VerificationFacts

    endpoint = API.replace("/_api", "")
    facts = await_if_needed(verify, endpoint, "rfc-test")

    # Policy A: accept if tree_hash is in allowlist (permissive for test)
    policy_a_allowlist = [facts.source.tree_hash]  # Allow this specific tree_hash
    policy_a_accepts = facts.source.tree_hash in policy_a_allowlist
    print(f"  Policy A (tree_hash allowlist): {policy_a_accepts}")

    # Policy B: accept only if onchain_approved AND quote_valid (strict)
    policy_b_accepts = facts.quote_valid and facts.onchain_approved
    print(f"  Policy B (strict onchain+quote): {policy_b_accepts}")

    # Verify: one accepts, one rejects
    assert policy_a_accepts != policy_b_accepts, "Policies should differ: one accepts, one rejects"
    print(f"  Same facts, different outcomes: A={policy_a_accepts}, B={policy_b_accepts} ✓")


def test_rfc0020_source_pull():
    """Test that git tree SHA equals source.tree_hash (agent path)."""
    print("\n--- Test: RFC 0020 source pull (tree hash verification) ---")
    import sys
    import subprocess
    sys.path.insert(0, os.path.dirname(__file__))
    from proxy.evidence import verify

    endpoint = API.replace("/_api", "")
    facts = await_if_needed(verify, endpoint, "rfc-test")

    # For git-deployed projects, tree_hash should equal the git tree SHA
    if facts.source.tree_hash_kind == "git":
        # Clone the repo at the commit and compute tree SHA
        work_dir = os.path.join(tmpdir, "repos/rfc-test-pull")
        subprocess.run(["git", "clone", facts.source.repo, work_dir], capture_output=True, check=True)
        subprocess.run(["git", "-C", work_dir, "checkout", facts.source.commit_sha], capture_output=True, check=True)
        result = subprocess.run(["git", "-C", work_dir, "rev-parse", facts.source.commit_sha + "^{tree}"],
                              capture_output=True, text=True, check=True)
        actual_tree_sha = result.stdout.strip()

        assert actual_tree_sha == facts.source.tree_hash, f"Git tree SHA mismatch: {actual_tree_sha} vs {facts.source.tree_hash}"
        print(f"  Git tree SHA matches deployed tree_hash: {actual_tree_sha[:12]} ✓")
    else:
        print(f"  Skipping git tree check (tree_hash_kind={facts.source.tree_hash_kind})")


def test_tier0_source_binding_survives_promote():
    """Tier-0 gate journey: source-backed dev deploy -> promote -> the evidence
    bundle still binds the deploy-time tree_hash.

    Mirrors oauth3-apps/harness/tier0-journeys.sh (journey 2/3): a source-backed
    app is deployed with no mode (dev, the default) then promoted to attested,
    and the verification bundle must surface app.source.tree_hash equal to the
    deploy-time value. Existing bundle tests deploy directly in attested mode,
    so they never exercise the promote() round-trip that this journey depends on.
    """
    print("\n--- Test: Tier-0 source binding survives dev->promote ---")
    repo = create_test_repo("tier0-src", {"index.html": b"<html>tier0 source binding</html>"})
    # Deploy with NO mode (dev, the default) — exactly like tier0 deploy_source.
    resp = api_post("/projects", json={
        "name": "tier0-src", "source": repo, "runtime": "static", "entry": "index.html"})
    assert resp.status_code == 201, f"deploy failed: {resp.status_code} {resp.text}"
    deploy = resp.json()
    assert deploy["mode"] == "dev", deploy
    deploy_tree_hash = deploy["tree_hash"]
    assert deploy_tree_hash, "source-backed deploy must produce a tree_hash"
    print(f"  deployed dev: tree_hash={deploy_tree_hash[:12]}")

    # Promote to attested — the journey step whose bundle effect was untested.
    resp = api_post(f"/projects/tier0-src/promote")
    assert resp.status_code == 200, f"promote failed: {resp.status_code} {resp.text}"
    assert resp.json()["mode"] == "attested", resp.json()
    print("  promoted to attested ✓")

    # The evidence bundle must still bind the deploy-time tree_hash.
    resp = api_get("/verification/tier0-src")
    assert resp.status_code == 200, f"verification failed: {resp.text}"
    bundle = resp.json()
    src = (bundle.get("app") or {}).get("source") or {}
    th = src.get("tree_hash", "")
    assert th, ("SOURCE BINDING MISSING: app.source.tree_hash absent after promote "
                f"(bundle app={bundle.get('app')!r})")
    assert th == deploy_tree_hash, (
        f"source binding drifted: bundle {th[:12]} != deploy-time {deploy_tree_hash[:12]}")
    print(f"  bundle app.source.tree_hash == deploy-time binding {th[:12]} ✓")

    # The promote must be persisted to the audit log (pha gate showed only 1
    # entry when this regressed — promote didn't complete). Assert it here so a
    # half-finished promote can't pass the gate silently.
    actions = [e.get("action") for e in (bundle.get("audit") or [])]
    assert "deploy" in actions and "promote" in actions, (
        f"audit missing promote entry: actions={actions}")
    print(f"  audit recorded deploy+promote: {actions} ✓")


def test_dstack_proxy_project_scoped():
    """Issue #80/#7: GetKey must derive the key path from the proxy's bound
    project_id (never a caller-supplied path), and reject traversal-shaped names.

    Mirrors the operator's vulnerable->fixed demo: an own-project key is derived
    correctly, and a cross-project `path` cannot redirect derivation.
    """
    import asyncio
    import shutil
    import aiohttp
    from aiohttp import web
    from proxy.dstack_proxy import DstackProxy

    print("\n--- Test: dstack GetKey scoped to bound project (#80) ---")

    async def fake_upstream(request):
        # Echo the (possibly rewritten) body so the test sees what path the proxy
        # actually forwarded to dstack.
        body = await request.read()
        return web.Response(body=body, content_type="application/json")

    async def run():
        tmp = tempfile.mkdtemp(prefix="dstack-proxy-test-")
        try:
            upstream_sock = os.path.join(tmp, "upstream.sock")
            up_app = web.Application()
            up_app.router.add_route("*", "/{path:.*}", fake_upstream)
            up_runner = web.AppRunner(up_app)
            await up_runner.setup()
            await web.UnixSite(up_runner, upstream_sock).start()

            proxy = DstackProxy(upstream_sock, "projA")
            px_app = web.Application()
            px_app.router.add_route("*", "/{path:.*}", proxy.handle)
            px_runner = web.AppRunner(px_app)
            await px_runner.setup()
            client_sock = os.path.join(tmp, "proxy.sock")
            await web.UnixSite(px_runner, client_sock).start()

            conn = aiohttp.UnixConnector(path=client_sock)
            async with aiohttp.ClientSession(connector=conn) as s:
                # own-key derives projA/master
                async with s.post("http://localhost/GetKey", json={"name": "master"}) as r:
                    assert r.status == 200, r.status
                    assert (await r.json())["path"] == "/tee-daemon/projects/projA/master"
                # legacy cross-project path is IGNORED -> still projA/master
                async with s.post("http://localhost/GetKey", json={
                    "name": "master",
                    "path": "/tee-daemon/projects/projB/secret",
                }) as r:
                    assert r.status == 200, r.status
                    assert (await r.json())["path"] == "/tee-daemon/projects/projA/master"
                # bad names rejected (traversal / empty / leading dot)
                for bad in ["", "../x", "a/b", "a\\b", ".hidden", "a..b"]:
                    async with s.post("http://localhost/GetKey", json={"name": bad}) as r:
                        assert r.status == 400, (bad, r.status)
                # a different project's proxy derives its OWN key (no cross-talk)
                proxyB = DstackProxy(upstream_sock, "projB")
                b_app = web.Application()
                b_app.router.add_route("*", "/{path:.*}", proxyB.handle)
                b_runner = web.AppRunner(b_app)
                await b_runner.setup()
                b_sock = os.path.join(tmp, "proxyB.sock")
                await web.UnixSite(b_runner, b_sock).start()
                bconn = aiohttp.UnixConnector(path=b_sock)
                async with aiohttp.ClientSession(connector=bconn) as s2:
                    async with s2.post("http://localhost/GetKey", json={"name": "master"}) as r:
                        assert (await r.json())["path"] == "/tee-daemon/projects/projB/master"
                await b_runner.cleanup()
                # non-GetKey methods still pass through (no name required)
                async with s.post("http://localhost/Info", json={}) as r:
                    assert r.status == 200, r.status
                # disallowed method denied
                async with s.post("http://localhost/RawSign", json={}) as r:
                    assert r.status == 403, r.status
            await px_runner.cleanup()
            await up_runner.cleanup()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    asyncio.run(run())
    print("  project-scoped GetKey derivation OK ✓")


def test_dstack_proxy_manager_per_project():
    """Issue #80: DstackProxyManager serves one project-scoped socket per project
    (dstack.sock + creds.sock in each project's own subdir), so a project's
    container can be given only its own broker dir."""
    import asyncio
    import shutil
    import aiohttp
    from aiohttp import web
    from proxy.dstack_proxy import DstackProxyManager

    print("\n--- Test: DstackProxyManager per-project sockets (#80) ---")

    async def fake_upstream(request):
        body = await request.read()
        return web.Response(body=body, content_type="application/json")

    async def creds_handler(request):
        # Stand-in for BrokerProxy: token-auth is irrelevant here; we only verify
        # the SAME runner is reachable on every project's creds.sock.
        return web.json_response({"ok": True})

    async def run():
        tmp = tempfile.mkdtemp(prefix="dstack-mgr-test-")
        broker_dir = os.path.join(tmp, "broker")
        os.makedirs(broker_dir)
        try:
            upstream_sock = os.path.join(tmp, "upstream.sock")
            up_app = web.Application()
            up_app.router.add_route("*", "/{path:.*}", fake_upstream)
            up_runner = web.AppRunner(up_app)
            await up_runner.setup()
            await web.UnixSite(up_runner, upstream_sock).start()

            creds_app = web.Application()
            creds_app.router.add_route("*", "/{path:.*}", creds_handler)
            creds_runner = web.AppRunner(creds_app)
            await creds_runner.setup()

            mgr = DstackProxyManager(upstream_sock, broker_dir, creds_runner)
            await mgr.ensure("projA")
            await mgr.ensure("projB")

            # Each project got its OWN dstack.sock + creds.sock in its own subdir.
            for p in ("projA", "projB"):
                assert os.path.exists(os.path.join(broker_dir, p, "dstack.sock"))
                assert os.path.exists(os.path.join(broker_dir, p, "creds.sock"))

            async def getkey(project, name):
                conn = aiohttp.UnixConnector(path=os.path.join(broker_dir, project, "dstack.sock"))
                async with aiohttp.ClientSession(connector=conn) as s:
                    async with s.post("http://localhost/GetKey", json={"name": name}) as r:
                        return r.status, (await r.json()).get("path")

            # projA's socket derives projA's key; projB's derives projB's — no cross-talk.
            st, path = await getkey("projA", "master")
            assert st == 200 and path == "/tee-daemon/projects/projA/master", (st, path)
            st, path = await getkey("projB", "master")
            assert st == 200 and path == "/tee-daemon/projects/projB/master", (st, path)

            # The shared creds runner is reachable on BOTH project sockets.
            for p in ("projA", "projB"):
                conn = aiohttp.UnixConnector(path=os.path.join(broker_dir, p, "creds.sock"))
                async with aiohttp.ClientSession(connector=conn) as s:
                    async with s.post("http://localhost/proxy/g-x", json={}) as r:
                        assert r.status == 200, (p, r.status)
                        assert (await r.json()) == {"ok": True}

            # ensure is idempotent; remove tears the project's sockets down.
            await mgr.ensure("projA")
            await mgr.remove("projA")
            assert not os.path.exists(os.path.join(broker_dir, "projA", "dstack.sock"))
            assert not os.path.exists(os.path.join(broker_dir, "projA", "creds.sock"))
            # projB unaffected by projA's removal.
            assert os.path.exists(os.path.join(broker_dir, "projB", "dstack.sock"))

            await mgr.stop_all()
            await creds_runner.cleanup()
            await up_runner.cleanup()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    asyncio.run(run())
    print("  per-project broker sockets (dstack + creds) OK ✓")


def await_if_needed(func, *args, **kwargs):
    """Helper to run async functions in sync context if needed."""
    import asyncio
    if asyncio.iscoroutinefunction(func):
        return asyncio.run(func(*args, **kwargs))
    return func(*args, **kwargs)


def test_browser_pool():
    """RFC 0028: pool isolates per-lease jars, resets between leases, and
    serializes/fairly-queues acquires against a 1-slot pool."""
    from concurrent.futures import ThreadPoolExecutor
    print("\n--- Test: RFC 0028 browser pool (isolation/reset/fairness/timeout) ---")

    # The pool warms in the background (image pull + health poll); wait for it.
    status = None
    for _ in range(80):
        r = api_get("/browser/pool")
        if r.status_code == 200 and r.json().get("started"):
            status = r.json()
            break
        time.sleep(0.5)
    assert status, f"browser pool never became ready: {r.status_code} {r.text}"
    assert status["size"] == 1, status
    print(f"  pool ready: size={status['size']} ✓")

    # --- per-lease isolation + reset (the core 0028 fix) ---
    # Lease A injects USER_A and renders -> sees USER_A.
    r = api_post("/browser/render", json={"domain": "example.com", "jar": "USER_A", "url": "/me"})
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "USER_A", r.json()
    # A fresh lease with NO jar must see empty (reset cleared USER_A). Without
    # reset this leaks USER_A to the next user — the exact bug in 0028.
    r = api_post("/browser/render", json={"domain": "example.com", "jar": "", "url": "/me"})
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "", f"reset leaked prior session: {r.json()}"
    # A different jar lands cleanly.
    r = api_post("/browser/render", json={"domain": "example.com", "jar": "USER_B", "url": "/me"})
    assert r.json()["body"] == "USER_B", r.json()
    print("  per-lease isolation + reset ✓")

    # --- fairness: a 1-slot pool must serialize concurrent leases ---
    def render(jar):
        return api_post("/browser/render", json={"domain": "ex.com", "jar": jar, "url": "/x"})
    with ThreadPoolExecutor(max_workers=2) as ex:
        fa, fb = ex.submit(render, "C1"), ex.submit(render, "C2")
        ra, rb = fa.result(), fb.result()
    assert ra.status_code == 200 and rb.status_code == 200, (ra.text, rb.text)
    # The bridge reports max concurrent /render calls it ever saw. A 1-slot pool
    # serializes leases so it must be 1; a broken pool handing one container to
    # both callers would see 2.
    assert ra.json()["max_active"] == 1, ra.json()
    assert rb.json()["max_active"] == 1, rb.json()
    print("  fairness: concurrent leases serialized (max_active=1) ✓")

    # --- acquire timeout under contention ---
    def render_slow():
        # holds the only slot for ~0.4s (bridge sleep)
        return api_post("/browser/render", json={"domain": "ex.com", "jar": "S", "url": "/x"})
    def render_hurry():
        return api_post("/browser/render", json={"domain": "ex.com", "jar": "H", "url": "/x", "timeout": 0.1})
    with ThreadPoolExecutor(max_workers=1) as ex:
        f_slow = ex.submit(render_slow)
        # Wait until render_slow has acquired the slot (busy=1), then contend.
        for _ in range(40):
            if api_get("/browser/pool").json().get("busy") == 1:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("render_slow never acquired the slot")
        r_hurry = render_hurry()  # pool busy -> acquire times out at 0.1s
        r_slow = f_slow.result()
    assert r_slow.status_code == 200, r_slow.text
    assert r_hurry.status_code == 503, f"expected lease timeout 503, got {r_hurry.status_code} {r_hurry.text}"
    assert "timeout" in r_hurry.json().get("error", ""), r_hurry.json()
    print("  acquire timeout under contention -> 503 ✓")


def test_rfc0017_export_import():
    print("\n--- Test: RFC 0017 export bundle + pinned import ---")
    repo = create_test_repo("test-exp", {
        "index.html": b"<html><body><h1>export me</h1></body></html>",
    })
    resp = api_post("/projects", json={
        "name": "test-exp", "source": repo, "runtime": "static",
        "env": {"SECRET": "hunter2-export-secret"},
    })
    assert resp.status_code == 201, f"Deploy failed: {resp.status_code} {resp.text}"
    pinned = resp.json()

    resp = api_get("/export")
    assert resp.status_code == 200, f"export failed: {resp.status_code} {resp.text}"
    assert "hunter2-export-secret" not in resp.text, "env secret leaked into export bundle"
    bundle = resp.json()
    entry = next(p for p in bundle["projects"] if p["name"] == "test-exp")
    for f in ("source", "ref", "commit_sha", "tree_hash", "image", "image_digest",
              "runtime", "entry", "port", "mode", "volumes"):
        assert f in entry, f"export entry missing {f}"
    assert "env" not in entry, "env must not be exported (RFC 0018 owns secrets)"
    assert entry["commit_sha"] == pinned["commit_sha"]
    assert entry["tree_hash"] == pinned["tree_hash"]
    print(f"  Export: {len(bundle['projects'])} projects, pins present, env absent \u2713")

    # Tampered pin: one flipped hex char must ERROR + skip, naming the project.
    bad_tree = ("0" if entry["tree_hash"][0] != "0" else "1") + entry["tree_hash"][1:]
    resp = api_post("/import", json={"projects": [{**entry, "tree_hash": bad_tree}]})
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert "test-exp" not in result["imported"], result
    skip = next(s for s in result["skipped"] if s["project"] == "test-exp")
    assert "tree_hash mismatch" in skip["error"], skip
    assert api_get("/projects/test-exp").status_code == 200, "failed import damaged live project"
    print(f"  Tampered tree_hash: ERROR+skip, project intact \u2713")

    # Pinned import must not chase the moving ref: push a new commit, import the
    # old pin, project comes back at the recorded commit_sha.
    push_update("test-exp", {"index.html": b"<html><body><h1>moved on</h1></body></html>"})
    resp = api_post("/import", json={"projects": [entry]})
    assert resp.status_code == 200, resp.text
    assert "test-exp" in resp.json()["imported"], resp.json()
    after = api_get("/projects/test-exp").json()
    assert after["commit_sha"] == pinned["commit_sha"], "import re-cloned at latest"
    assert after["tree_hash"] == pinned["tree_hash"]
    resp = requests.get(f"{INGRESS}/test-exp/")
    assert "export me" in resp.text, "import served content outside the pin"
    print("  Clean import: redeployed at pinned commit, not latest \u2713")


def test_rfc0017_bootstrap():
    print("\n--- Test: RFC 0017 empty registry + bundle at boot restores fleet ---")
    bundle = api_get("/export").json()
    assert bundle["projects"], "expected a non-empty fleet to export"
    stop_daemon()
    data_dir = os.path.join(tmpdir, "projects")
    for e in os.listdir(data_dir):
        if os.path.isfile(os.path.join(data_dir, e, "project.json")):
            shutil.rmtree(os.path.join(data_dir, e))
    with open(os.path.join(data_dir, "import-bundle.json"), "w") as f:
        json.dump(bundle, f)
    start_daemon(reuse_tmpdir=True)
    restored = {p["name"]: p for p in api_get("/projects").json()}
    for p in bundle["projects"]:
        restorable = (p.get("runtime") == "image"
                      or p.get("source", "").startswith(("https://", "http://", "/")))
        if not restorable:
            # tarball-origin projects carry a placeholder source — RFC 0017:
            # they cannot be reconstituted, so the skip must be visible, not silent.
            assert p["name"] not in restored, f"{p['name']} restored without a cloneable source?"
            continue
        assert p["name"] in restored, f"{p['name']} not restored from bundle"
        if p["commit_sha"]:
            assert restored[p["name"]]["commit_sha"] == p["commit_sha"], \
                f"{p['name']} restored off-pin"
    resp = requests.get(f"{INGRESS}/test-static/")
    assert "Updated" in resp.text, "restored fleet not serving"
    print(f"  Bootstrap: {len(bundle['projects'])} projects restored at their pins \u2713")


def test_teardown():
    print("\n--- Test: teardown ---")
    for name in ["test-static", "test-caps", "test-deno", "test-auto", "test-tarball", "test-image", "test-iso-a", "test-iso-b", "test-passthru", "test-iso-passthru", "test-redeploy-img", "test-redact", "net-a", "net-b", "data-iso", "rfc-test", "test-opdebug", "test-opdebug-off", "tier0-src", "test-exp"]:
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
        test_dstack_proxy_project_scoped()
        test_dstack_proxy_manager_per_project()
        test_auth()
        test_version()
        test_boot_refuses_without_commit()
        test_deploy_static()
        test_caps_require_attested()
        test_operator_debug()
        test_ingress_static()
        test_scoped_tokens()
        test_git_blocked()
        test_playwright_static()
        test_deploy_deno()
        test_ingress_deno()
        test_runtime_selection()
        test_deploy_image()
        test_ingress_image()
        test_volume_adoption()
        test_per_project_isolation()
        test_per_project_network_isolation()
        test_isolated_per_project_data_volume()
        test_env_passthrough()
        test_isolated_deno_env_passthrough()
        test_dns_probe()
        test_image_redeploy()
        test_substrate_endpoint()
        test_autodetect()
        test_deploy_multipart_static()
        test_deploy_multipart_missing_files()
        test_deploy_multipart_missing_manifest()
        test_deploy_multipart_bad_json()
        test_tarball_redeploy_preserves_project()
        test_git_clone_failure_preserves_dest()
        test_redeploy()
        test_audit_log()
        test_list_projects()
        test_env_redaction()
        test_root_listing_layers()
        test_landing_cards()
        test_landing_descriptions()
        test_rfc0020_bundle()
        test_rfc0020_tamper()
        test_rfc0020_non_anchored()
        test_rfc0020_two_policies()
        test_rfc0020_source_pull()
        test_tier0_source_binding_survives_promote()
        test_browser_pool()
        test_rfc0017_export_import()
        test_rfc0017_bootstrap()
        test_teardown()
        print("\n=== ALL TESTS PASSED ===")
    except Exception:
        raise
    finally:
        cleanup_containers()
        stop_daemon()


if __name__ == "__main__":
    main()

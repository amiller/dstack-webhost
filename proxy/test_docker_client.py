"""Unit tests for create_container's HostConfig shaping (no daemon, no docker).

runsc containers must carry explicit Dns: Docker's embedded 127.0.0.11 resolver
is dead under the gVisor netstack (issue #2). Every other runtime must stay
untouched so runc apps keep Docker's embedded resolver."""

import asyncio
import base64
import json
import os

from .docker_client import GVISOR_DNS, DockerClient, _registry_auth


def _capturing_client():
    captured = {}

    async def fake_json_request(method, path, timeout=300, json=None, **kw):
        captured["body"] = json
        return 201, {"Id": "deadbeef"}

    dc = DockerClient("/nonexistent/docker.sock")
    dc._json_request = fake_json_request
    return captured, dc


def _create(dc, runtime):
    asyncio.run(dc.create_container(
        "c", "img", [], [], {}, "tee-proj-x-dev", runtime=runtime))


def test_runsc_create_carries_gvisor_dns():
    captured, dc = _capturing_client()
    _create(dc, "runsc")
    hc = captured["body"]["HostConfig"]
    assert hc["Runtime"] == "runsc"
    assert hc["Dns"] == GVISOR_DNS


def test_runsc_variants_carry_gvisor_dns():
    captured, dc = _capturing_client()
    _create(dc, "runsc-hostuds")
    hc = captured["body"]["HostConfig"]
    assert hc["Runtime"] == "runsc-hostuds"
    assert hc["Dns"] == GVISOR_DNS


def test_runc_and_shared_creates_keep_embedded_dns():
    for runtime in ("runc", ""):
        captured, dc = _capturing_client()
        _create(dc, runtime)
        hc = captured["body"]["HostConfig"]
        assert "Dns" not in hc, f"runtime={runtime!r} must keep the embedded resolver"


def _pull_capturing_client():
    captured = {}

    async def fake_raw(method, path, timeout=300, headers=None, **kw):
        captured["path"] = path
        captured["headers"] = headers or {}
        return 200, b""

    dc = DockerClient("/nonexistent/docker.sock")
    dc._raw_request = fake_raw
    return captured, dc


def _with_env(**kv):
    saved = {k: os.environ.get(k) for k in kv}
    os.environ.update({k: v for k, v in kv.items() if v is not None})
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_registry_auth_public_none_ghcr_from_env():
    saved = _with_env(GHCR_USERNAME="u", GHCR_TOKEN="t", REGISTRY_AUTHS=None)
    try:
        assert _registry_auth("node:24-slim") is None
        auth = json.loads(base64.b64decode(_registry_auth("ghcr.io/o/r@sha256:abc")))
        assert auth == {"username": "u", "password": "t", "serveraddress": "ghcr.io"}
    finally:
        _restore_env(saved)


def test_pull_private_ghcr_sends_auth_and_splits_digest():
    saved = _with_env(GHCR_USERNAME="u", GHCR_TOKEN="t")
    try:
        captured, dc = _pull_capturing_client()
        asyncio.run(dc.pull("ghcr.io/o/r@sha256:abc123"))
        assert "fromImage=ghcr.io/o/r&tag=sha256:abc123" in captured["path"]
        assert "X-Registry-Auth" in captured["headers"]
    finally:
        _restore_env(saved)


def test_pull_public_sends_no_auth():
    saved = _with_env(GHCR_USERNAME=None, GHCR_TOKEN=None, REGISTRY_AUTHS=None)
    try:
        captured, dc = _pull_capturing_client()
        asyncio.run(dc.pull("node:24-slim"))
        assert "fromImage=node:24-slim" in captured["path"]
        assert "X-Registry-Auth" not in captured["headers"]
    finally:
        _restore_env(saved)

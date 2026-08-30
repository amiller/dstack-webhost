"""Unit tests for create_container's HostConfig shaping (no daemon, no docker).

runsc containers must carry explicit Dns: Docker's embedded 127.0.0.11 resolver
is dead under the gVisor netstack (issue #2). Every other runtime must stay
untouched so runc apps keep Docker's embedded resolver."""

import asyncio

from .docker_client import GVISOR_DNS, DockerClient


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


def _state_client(status: int, data: dict) -> DockerClient:
    dc = DockerClient("/nonexistent/docker.sock")

    async def fake_json_request(method, path, timeout=300, **kw):
        return status, data

    dc._json_request = fake_json_request
    return dc


def test_container_state_maps_inspect_to_status_fields():
    dc = _state_client(200, {"Id": "abc123", "RestartCount": 3,
                             "State": {"Running": False, "Status": "exited", "ExitCode": 1}})
    assert asyncio.run(dc.container_state("tee-image-x-dev")) == {
        "running": False, "container_id": "abc123", "container_state": "exited",
        "exit_code": 1, "restart_count": 3}

    dc = _state_client(200, {"Id": "abc123", "RestartCount": 0,
                             "State": {"Running": True, "Status": "running", "ExitCode": 0}})
    live = asyncio.run(dc.container_state("tee-image-x-dev"))
    assert live["running"] is True
    assert live["exit_code"] is None, "ExitCode carries no information while running"


def test_container_state_only_404_is_missing():
    dc = _state_client(404, {"message": "No such container: tee-image-x-dev"})
    assert asyncio.run(dc.container_state("tee-image-x-dev")) is None
    try:
        asyncio.run(_state_client(500, {"message": "boom"})
                    .container_state("tee-image-x-dev"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("an engine error must propagate, not read as missing")

"""Unit tests for create_container's HostConfig shaping (no daemon, no docker).

runsc containers must carry explicit Dns: Docker's embedded 127.0.0.11 resolver
is dead under the gVisor netstack (issue #2). Every other runtime must stay
untouched so runc apps keep Docker's embedded resolver."""

import asyncio
import io
import tarfile

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


def test_exec_decodes_docker_multiplexed_output():
    dc = DockerClient("/nonexistent/docker.sock")
    async def create(*args, **kwargs):
        return 200, {"Id": "exec-id"}
    async def start(*args, **kwargs):
        return 200, b"\x01\x00\x00\x00\x00\x00\x00\x03out"
    dc._json_request = create
    dc._raw_request = start
    assert asyncio.run(dc.exec("deadbeef", ["cat", "/data/a"])) == "out"


def test_read_data_file_extracts_one_regular_file():
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode="w:") as archive:
        data = b"value"
        info = tarfile.TarInfo("a.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    dc = DockerClient("/nonexistent/docker.sock")
    async def raw(*args, **kwargs):
        return 200, body.getvalue()
    dc._raw_request = raw
    assert asyncio.run(dc.read_data_file("deadbeef", "a.txt")) == b"value"

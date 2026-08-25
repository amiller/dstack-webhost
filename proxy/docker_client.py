"""Thin async Docker Engine client over Unix socket."""

import logging

import aiohttp

log = logging.getLogger(__name__)

# Docker's embedded resolver (127.0.0.11, forced on user-defined bridge
# networks) is dead under gVisor: the Sentry netstack never applies the host
# iptables DNAT that makes it reachable under runc. gVisor containers get
# explicit routable resolvers instead — the queried hostname is already
# disclosed by SNI, and loopback stubs (e.g. systemd-resolved 127.0.0.53)
# are dead the same way, so the host's only usable upstream (8.8.8.8) plus
# one public fallback.
GVISOR_DNS = ["8.8.8.8", "1.1.1.1"]


def _demux_docker_stream(body: bytes) -> str:
    """Strip Docker's multiplexed log framing. Without a TTY the engine prefixes
    every chunk with an 8-byte header [stream(1), 0,0,0, size(be32)]; those header
    bytes otherwise show up as garbage (the leading R/B/A seen in raw output). If
    the buffer isn't cleanly framed (TTY containers emit raw bytes), fall back to a
    plain decode rather than mangling it."""
    out, i, n = [], 0, len(body)
    while i + 8 <= n:
        stream = body[i]
        size = int.from_bytes(body[i + 4:i + 8], "big")
        if stream not in (0, 1, 2) or i + 8 + size > n:
            return body.decode("utf-8", errors="replace")
        out.append(body[i + 8:i + 8 + size])
        i += 8 + size
    if i != n:
        return body.decode("utf-8", errors="replace")
    return b"".join(out).decode("utf-8", errors="replace")


class DockerClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    async def _json_request(self, method: str, path: str, timeout: int = 300, **kwargs) -> tuple[int, dict | list]:
        conn = aiohttp.UnixConnector(path=self.socket_path)
        ct = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(connector=conn, timeout=ct) as session:
            async with session.request(method, f"http://localhost{path}", **kwargs) as resp:
                data = await resp.json()
                return resp.status, data

    async def _raw_request(self, method: str, path: str, timeout: int = 300, **kwargs) -> tuple[int, bytes]:
        conn = aiohttp.UnixConnector(path=self.socket_path)
        ct = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(connector=conn, timeout=ct) as session:
            async with session.request(method, f"http://localhost{path}", **kwargs) as resp:
                body = await resp.read()
                return resp.status, body

    async def create_container(self, name: str, image: str, cmd: list[str],
                               binds: list[str], labels: dict, network: str,
                               env: list[str] | None = None,
                               runtime: str = "",
                               restart_policy: dict | None = None,
                               cap_add: list[str] | None = None,
                               devices: list[str] | None = None) -> str:
        host_config: dict = {"Binds": binds}
        if runtime:
            host_config["Runtime"] = runtime
        if runtime == "runsc":
            host_config["Dns"] = GVISOR_DNS
        if restart_policy:
            host_config["RestartPolicy"] = restart_policy
        if cap_add:
            host_config["CapAdd"] = cap_add
        if devices:
            host_config["Devices"] = [
                {"PathOnHost": d, "PathInContainer": d, "CgroupPermissions": "rwm"}
                for d in devices]
        body = {
            "Image": image,
            "Cmd": cmd or None,
            "Labels": labels,
            "Env": env or [],
            "HostConfig": host_config,
            "NetworkingConfig": {"EndpointsConfig": {network: {}}},
        }
        status, data = await self._json_request("POST", f"/containers/create?name={name}", json=body)
        if status >= 400:
            raise RuntimeError(f"create_container failed ({status}): {data}")
        return data["Id"]

    async def start(self, cid: str):
        status, _ = await self._raw_request("POST", f"/containers/{cid}/start")
        if status >= 400 and status != 304:
            raise RuntimeError(f"start failed ({status})")

    async def stop(self, cid: str, timeout: int = 5):
        await self._raw_request("POST", f"/containers/{cid}/stop?t={timeout}")

    async def remove(self, cid: str, force: bool = True):
        await self._raw_request("DELETE", f"/containers/{cid}?force={'true' if force else 'false'}")

    async def info(self) -> dict:
        status, data = await self._json_request("GET", "/info")
        if status >= 400:
            raise RuntimeError(f"info failed ({status}): {data}")
        return data

    async def inspect(self, cid: str) -> dict:
        status, data = await self._json_request("GET", f"/containers/{cid}/json")
        if status >= 400:
            raise RuntimeError(f"inspect failed ({status}): {data}")
        return data

    async def logs(self, cid: str, tail: int = 100) -> str:
        _, body = await self._raw_request("GET", f"/containers/{cid}/logs?stdout=true&stderr=true&tail={tail}")
        return _demux_docker_stream(body)

    # A pull that never returns must not be able to stall a boot. Docker Hub throttling an
    # anonymous pull looks exactly like this, and 300s of it per broken tenant is a long time
    # to hold the substrate. Recovery treats a failure here as "skip that project".
    PULL_TIMEOUT = 120

    async def pull(self, image: str):
        status, body = await self._raw_request(
            "POST", f"/images/create?fromImage={image}", timeout=self.PULL_TIMEOUT)
        if status >= 400:
            # Registry unreachable / rate-limited is fine if the image is already
            # cached locally — use the cached image rather than blocking the deploy.
            if await self.image_digest(image):
                return
            raise RuntimeError(f"pull failed ({status}): {image}")

    async def container_ip(self, cid: str, network: str) -> str:
        data = await self.inspect(cid)
        return data["NetworkSettings"]["Networks"][network]["IPAddress"]

    async def image_digest(self, image: str) -> str:
        status, data = await self._json_request("GET", f"/images/{image}/json")
        if status >= 400:
            return ""
        return data.get("Id", "")

    async def connect_network(self, container: str, network: str, aliases: list[str] | None = None):
        body = {"Container": container}
        if aliases:
            body["EndpointConfig"] = {"Aliases": aliases}
        status, data = await self._raw_request(
            "POST", f"/networks/{network}/connect", json=body)
        if status >= 400 and b"already exists" not in data:
            raise RuntimeError(f"connect_network failed ({status}): {data!r}")

    async def disconnect_network(self, container: str, network: str):
        """Detach; the daemon joins every project network, and a network it is still on
        cannot be removed. Not-found either way is the desired end state, not an error."""
        status, data = await self._raw_request(
            "POST", f"/networks/{network}/disconnect",
            json={"Container": container, "Force": True})
        if status >= 400 and status != 404 and b"not found" not in data.lower():
            raise RuntimeError(f"disconnect_network failed ({status}): {data!r}")

    async def run_build(self, image: str, cmd: list[str], binds: list[str]) -> tuple[int, str]:
        body = {"Image": image, "Cmd": cmd, "HostConfig": {"Binds": binds}}
        status, data = await self._json_request("POST", "/containers/create", json=body)
        if status >= 400:
            raise RuntimeError(f"build create failed ({status}): {data}")
        cid = data["Id"]
        await self.start(cid)
        _, wait_data = await self._json_request("POST", f"/containers/{cid}/wait", timeout=600)
        exit_code = wait_data.get("StatusCode", -1)
        logs = await self.logs(cid, tail=200)
        await self.remove(cid, force=True)
        return exit_code, logs

    async def create_network(self, name: str):
        """Idempotent: 201 on create, 409 if exists, both fine."""
        status, data = await self._json_request(
            "POST", "/networks/create", json={"Name": name, "Driver": "bridge"})
        if status not in (201, 409):
            raise RuntimeError(f"create_network failed ({status}): {data}")

    async def remove_network(self, name: str) -> bool:
        """Remove a network. True if it went away, False if Docker still wants it.

        404 counts as success — the goal is that it is gone. 403/409 mean containers
        are still attached, which is a live tenant and not ours to disturb."""
        status, data = await self._json_request("DELETE", f"/networks/{name}")
        if status in (204, 404):
            return True
        if status in (403, 409):
            return False
        raise RuntimeError(f"remove_network failed ({status}): {data}")

    async def list_networks(self, prefix: str) -> list[str]:
        status, data = await self._json_request("GET", "/networks")
        if status >= 400:
            raise RuntimeError(f"list_networks failed ({status}): {data}")
        return [n["Name"] for n in data if n.get("Name", "").startswith(prefix)]

    async def network_is_empty(self, name: str) -> bool:
        """No containers attached. /networks/<name> populates Containers; the list
        endpoint does not, which is why this is a second call per candidate."""
        status, data = await self._json_request("GET", f"/networks/{name}")
        if status == 404:
            return False
        if status >= 400:
            raise RuntimeError(f"inspect network failed ({status}): {data}")
        return not (data.get("Containers") or {})

    async def ensure_volume(self, name: str):
        """Idempotent volume create — Docker returns 201 with existing data if it exists."""
        status, data = await self._json_request(
            "POST", "/volumes/create", json={"Name": name})
        if status >= 400:
            raise RuntimeError(f"ensure_volume failed ({status}): {data}")

    async def container_exists(self, name: str) -> str | None:
        status, data = await self._json_request("GET", f"/containers/{name}/json")
        if status == 200:
            return data["Id"]
        return None

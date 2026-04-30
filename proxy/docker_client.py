"""Thin async Docker Engine client over Unix socket."""

import logging

import aiohttp

log = logging.getLogger(__name__)


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
                               runtime: str = "") -> str:
        host_config: dict = {"Binds": binds}
        if runtime:
            host_config["Runtime"] = runtime
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

    async def inspect(self, cid: str) -> dict:
        status, data = await self._json_request("GET", f"/containers/{cid}/json")
        if status >= 400:
            raise RuntimeError(f"inspect failed ({status}): {data}")
        return data

    async def logs(self, cid: str, tail: int = 100) -> str:
        _, body = await self._raw_request("GET", f"/containers/{cid}/logs?stdout=true&stderr=true&tail={tail}")
        return body.decode("utf-8", errors="replace")

    async def pull(self, image: str):
        status, body = await self._raw_request("POST", f"/images/create?fromImage={image}")
        if status >= 400:
            raise RuntimeError(f"pull failed ({status}): {image}")

    async def container_ip(self, cid: str, network: str) -> str:
        data = await self.inspect(cid)
        return data["NetworkSettings"]["Networks"][network]["IPAddress"]

    async def image_digest(self, image: str) -> str:
        status, data = await self._json_request("GET", f"/images/{image}/json")
        if status >= 400:
            return ""
        return data.get("Id", "")

    async def connect_network(self, container: str, network: str):
        status, data = await self._raw_request(
            "POST", f"/networks/{network}/connect", json={"Container": container})
        if status >= 400 and b"already exists" not in data:
            raise RuntimeError(f"connect_network failed ({status}): {data!r}")

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

"""Browser render pool (RFC 0028) — warm, isolated, fairly-leased browser containers.

The logged-in browser is a first-class daemon runtime: a *pool* of isolated
browser-bridge containers on the pod's internal network. A browser-path read
**leases** one, the requester's cookie jar is injected for that lease only, the
browser is driven, and the container is **reset** (cookies/storage cleared)
before it returns to the pool. This is the multi-user fix for the old single
shared CVM whose `browserFeed` read whatever was logged in (a non-owner saw the
owner's timeline), with no isolation, reset, or fairness.

Contract — a browser-bridge container is any image that speaks this HTTP surface
(a real Neko/Chromium+bridge image, or a test double):
    GET  /health           -> 200 {ok: true}
    POST /session  {domain, cookies}   -> 200 {ok: true}   # inject jar (domain-scoped)
    POST /render   {domain, url}       -> 200 {body, ...}  # drive; body reflects active session
    POST /reset                            -> 200 {ok: true}   # clear ALL cookies/storage/nav

The jar *source* (how acquire() got the requester's jar) is the credential
broker's job (RFC 0018); the pool's job is what happens to a jar for the life of
a lease: isolated, fair, reset. So acquire() takes the already-resolved jar; the
broker wiring is a typed seam, not built here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass

import aiohttp

from .docker_client import DockerClient
from .tracker import ContainerTracker

log = logging.getLogger(__name__)

# Bridge HTTP contract paths (see module docstring).
HEALTH = "/health"
SESSION = "/session"
RENDER = "/render"
RESET = "/reset"

DEFAULT_PORT = 3000
DEFAULT_SIZE = 2
DEFAULT_LEASE_TTL = 30.0          # max seconds a lease may hold a container
DEFAULT_ACQUIRE_TIMEOUT = 10.0    # max seconds to wait for a free container
DEFAULT_DRIVE_TIMEOUT = 90.0      # per-drive (navigate/render) cap, seconds
READINESS_DEADLINE = 30.0         # per-container health-poll cap at start, seconds


class LeaseTimeout(Exception):
    """No browser became free within the acquire deadline (pool exhausted /
    starved). Raised, not swallowed — the caller decides retry/back-off."""


@dataclass
class Lease:
    id: str
    slot: int          # pool index of the container driving this lease
    ip: str
    port: int
    domain: str
    deadline: float    # wall-clock seconds after which the sweeper force-releases

    def expired(self, now: float) -> bool:
        return now >= self.deadline


class BrowserPool:
    """A warm pool of browser-bridge containers.

    Fairness: an asyncio.Queue of free slots serializes acquires; a per-lease
    deadline (lease_ttl) bounds how long one caller can hold a container, and a
    background sweeper force-releases expired leases so a forgotten caller can't
    starve the pool. Per-lease isolation: acquire injects only this lease's jar;
    release resets the container before it re-enters the pool."""

    def __init__(self, docker: DockerClient, tracker: ContainerTracker,
                 image: str, cmd: list[str], binds: list[str],
                 port: int = DEFAULT_PORT, size: int = DEFAULT_SIZE,
                 network: str = "tee-apps-attested",
                 lease_ttl: float = DEFAULT_LEASE_TTL):
        self.docker = docker
        self.tracker = tracker
        self.image = image
        self.cmd = cmd
        self.binds = list(binds)
        self.port = port
        self.size = size
        self.network = network
        self.lease_ttl = lease_ttl

        self._free: asyncio.Queue[int] | None = None
        self._containers: list[dict] = []      # slot -> {cid, ip}
        self._leases: dict[str, Lease] = {}
        self._session: aiohttp.ClientSession | None = None
        self._sweeper: asyncio.Task | None = None
        self._started = False

    async def start(self):
        """Pull the bridge image and bring up `size` warm containers, then
        health-poll each until ready. Removes any stale `tee-browser-*`
        containers from a prior run first."""
        if self._started:
            return
        self._free = asyncio.Queue()
        self._session = aiohttp.ClientSession()
        log.info("Pulling browser image %s...", self.image)
        await self.docker.pull(self.image)

        labels = {"tee-proxy.managed": "true", "tee-daemon.browser": "true"}
        restart = {"Name": "on-failure", "MaximumRetryCount": 3}

        for slot in range(self.size):
            cname = self._cname(slot)
            existing = await self.docker.container_exists(cname)
            if existing:
                await self.docker.stop(existing)
                await self.docker.remove(existing)
                self.tracker.remove(existing)

            cid = await self.docker.create_container(
                cname, self.image, self.cmd, self.binds, labels, self.network,
                restart_policy=restart)
            await self.docker.start(cid)
            self.tracker.add(cid)
            ip = await self.docker.container_ip(cid, self.network)
            self._containers.append({"cid": cid, "ip": ip})
            await self._wait_ready(ip, cname)
            self._free.put_nowait(slot)
            log.info("Browser slot %d -> %s (%s:%d)", slot, cid[:12], ip, self.port)

        self._sweeper = asyncio.create_task(self._sweep_loop())
        self._started = True
        log.info("Browser pool ready: %d slot(s)", self.size)

    async def _wait_ready(self, ip: str, cname: str):
        deadline = time.monotonic() + READINESS_DEADLINE
        last = None
        while time.monotonic() < deadline:
            try:
                async with self._session.get(
                        f"http://{ip}:{self.port}{HEALTH}", timeout=2) as resp:
                    if resp.status == 200:
                        return
                    last = f"status {resp.status}"
            except Exception as e:
                last = str(e)
            await asyncio.sleep(0.5)
        raise RuntimeError(f"browser container {cname} not healthy: {last}")

    async def acquire(self, domain: str, jar: str,
                      timeout: float = DEFAULT_ACQUIRE_TIMEOUT) -> Lease:
        """Wait for a free container (fairness queue, bounded by `timeout`),
        then inject `jar` for `domain` only. Raises LeaseTimeout if none free."""
        assert self._started and self._free is not None, "pool not started"
        try:
            slot = await asyncio.wait_for(self._free.get(), timeout)
        except asyncio.TimeoutError:
            raise LeaseTimeout(
                f"no browser free within {timeout}s (size={self.size})")

        info = self._containers[slot]
        lease = Lease(
            id=f"bl-{uuid.uuid4().hex[:12]}", slot=slot, ip=info["ip"],
            port=self.port, domain=domain,
            deadline=time.time() + self.lease_ttl)
        try:
            await self._post(info["ip"], SESSION,
                             {"domain": domain, "cookies": jar})
        except Exception:
            # Injection failed — release the slot so it isn't stranded, then
            # surface the real error (never mask it).
            self._free.put_nowait(slot)
            raise
        self._leases[lease.id] = lease
        log.info("Lease %s -> slot %d (%s)", lease.id, slot, domain or "<none>")
        return lease

    async def drive(self, lease: Lease, op: dict,
                    timeout: float = DEFAULT_DRIVE_TIMEOUT) -> dict:
        """Run one browser op on the leased container (e.g. {url}). The
        container drives as the session injected at acquire."""
        if lease.id not in self._leases:
            raise RuntimeError(f"unknown/expired lease {lease.id}")
        op = {**op, "domain": lease.domain}
        return await self._post(lease.ip, RENDER, op, timeout=timeout)

    async def release(self, lease: Lease):
        """Reset the container (clear cookies/storage) and return it to the
        pool. Safe to call once per lease."""
        if not self._leases.pop(lease.id, None):
            return  # already released (e.g. by the sweeper)
        try:
            await self._post(lease.ip, RESET, {})
            log.info("Lease %s released (reset)", lease.id)
        except Exception as e:
            # Reset is the isolation guarantee, so a failed reset is loud: the
            # container may carry residue and must not be reused as-is. Drop it
            # from the free pool; a new container is created on next start().
            log.error("Lease %s reset FAILED on slot %d (%s) — slot drained",
                      lease.id, lease.slot, e)
            return
        self._free.put_nowait(lease.slot)

    async def render(self, domain: str, jar: str, url: str,
                     timeout: float = DEFAULT_ACQUIRE_TIMEOUT,
                     op: dict | None = None) -> dict:
        """One-shot lease → drive → release. The common browser-path read.
        `op` merges into the drive payload alongside `url`, so a caller can ask
        the bridge for more than a plain render (a driven task, a capture).
        The bridge's response is returned with the lease result attached
        ("lease": id/slot/domain/render_ms) so callers record what actually
        served them, not just that a render happened."""
        lease = await self.acquire(domain, jar, timeout)
        started = time.monotonic()
        try:
            payload = {"url": url}
            if op:
                payload.update(op)
            result = await self.drive(lease, payload)
            result["lease"] = {"id": lease.id, "slot": lease.slot,
                               "domain": lease.domain,
                               "render_ms": round((time.monotonic() - started) * 1000)}
            return result
        finally:
            await self.release(lease)

    def status(self) -> dict:
        free = self._free.qsize() if self._free else 0
        return {
            "started": self._started,
            "size": self.size,
            "free": free,
            "busy": self.size - free,
            "active_leases": len(self._leases),
            "image": self.image,
        }

    async def stop(self):
        if self._sweeper:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass
            self._sweeper = None
        for slot, info in enumerate(self._containers):
            try:
                await self.docker.stop(info["cid"])
                await self.docker.remove(info["cid"])
                self.tracker.remove(info["cid"])
            except Exception as e:
                log.warning("stop slot %d: %s", slot, e)
        self._containers = []
        self._leases = {}
        self._free = None
        if self._session:
            await self._session.close()
            self._session = None
        self._started = False

    async def _sweep_loop(self):
        """Force-release leases past their deadline so a forgotten caller can't
        hold a browser forever (fairness backstop)."""
        while True:
            await asyncio.sleep(max(1.0, self.lease_ttl / 4))
            now = time.time()
            for lease in list(self._leases.values()):
                if lease.expired(now):
                    log.warning("Lease %s expired (slot %d) — force release",
                                lease.id, lease.slot)
                    await self.release(lease)

    async def _post(self, ip: str, path: str, body: dict,
                    timeout: float = DEFAULT_DRIVE_TIMEOUT) -> dict:
        url = f"http://{ip}:{self.port}{path}"
        async with self._session.post(url, json=body, timeout=timeout) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(f"{path} -> {resp.status}: {data}")
            return data

    def _cname(self, slot: int) -> str:
        return f"tee-browser-{slot}"


def parse_binds(spec: str) -> list[str]:
    """`BROWSER_POOL_BINDS=a:/x:ro,b:/y` -> ['a:/x:ro', 'b:/y']."""
    return [b for b in (s.strip() for s in spec.split(",")) if b]

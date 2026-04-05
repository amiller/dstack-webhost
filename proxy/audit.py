"""Audit log + RTMR measurement extension for container operations."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict

import aiohttp

log = logging.getLogger(__name__)


@dataclass
class AuditEntry:
    timestamp: float
    action: str  # create, start, stop, remove, pull
    container_id: str = ""
    image: str = ""
    image_digest: str = ""
    detail: str = ""


class AuditLog:
    def __init__(self, dstack_socket: str | None = None):
        self.entries: list[AuditEntry] = []
        self.dstack_socket = dstack_socket

    async def record(self, entry: AuditEntry):
        self.entries.append(entry)
        log.info("AUDIT %s container=%s image=%s",
                 entry.action, entry.container_id[:12] if entry.container_id else "-",
                 entry.image or "-")
        await self._extend_rtmr(entry)

    async def _extend_rtmr(self, entry: AuditEntry):
        if not self.dstack_socket:
            return
        payload = json.dumps(asdict(entry), sort_keys=True)
        payload_hex = payload.encode().hex()
        body = {"event": f"tee-proxy:{entry.action}", "payload": payload_hex}
        try:
            conn = aiohttp.UnixConnector(path=self.dstack_socket)
            async with aiohttp.ClientSession(connector=conn) as session:
                async with session.post("http://localhost/EmitEvent", json=body) as resp:
                    if resp.status == 200:
                        log.info("RTMR extended: %s", entry.action)
                    else:
                        log.warning("EmitEvent returned %s", resp.status)
        except Exception as e:
            log.warning("Failed to extend RTMR: %s", e)

    def to_json(self) -> list[dict]:
        return [asdict(e) for e in self.entries]

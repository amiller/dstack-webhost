"""Audit log + RTMR measurement extension for container operations."""

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, asdict

import aiohttp

log = logging.getLogger(__name__)


@dataclass
class AuditEntry:
    timestamp: float
    action: str  # create, start, stop, remove, pull, deploy, teardown, promote
    container_id: str = ""
    image: str = ""
    image_digest: str = ""
    detail: str = ""


class AuditLogManager:
    """Per-project audit log manager with disk persistence."""

    def __init__(self, audit_dir: str):
        self.audit_dir = audit_dir
        os.makedirs(audit_dir, exist_ok=True)

    def _audit_file(self, project_name: str) -> str:
        return os.path.join(self.audit_dir, f"{project_name}.jsonl")

    def _load_entries(self, project_name: str) -> list[AuditEntry]:
        """Load audit entries from disk for a project."""
        entries = []
        audit_file = self._audit_file(project_name)
        if os.path.exists(audit_file):
            with open(audit_file, "r") as f:
                for line in f:
                    if line.strip():
                        try:
                            entries.append(AuditEntry(**json.loads(line)))
                        except Exception as e:
                            log.warning("Failed to parse audit entry: %s", e)
        return entries

    def _save_entry(self, project_name: str, entry: AuditEntry):
        """Append an audit entry to disk."""
        audit_file = self._audit_file(project_name)
        with open(audit_file, "a") as f:
            f.write(json.dumps(asdict(entry)) + "\n")

    def get_audit_log(self, project_name: str) -> "AuditLog":
        """Get an AuditLog instance for a specific project."""
        return AuditLog(project_name, self)

    def delete_audit_log(self, project_name: str):
        """Delete audit log for a project."""
        audit_file = self._audit_file(project_name)
        if os.path.exists(audit_file):
            os.remove(audit_file)


class AuditLog:
    """Project-specific audit log with RTMR measurement extension."""

    def __init__(self, project_name: str, manager: AuditLogManager,
                 dstack_socket: str | None = None):
        self.project_name = project_name
        self.manager = manager
        self.dstack_socket = dstack_socket

    async def record(self, entry: AuditEntry):
        """Record an audit entry and extend RTMR if configured."""
        self.manager._save_entry(self.project_name, entry)
        log.info("AUDIT %s project=%s container=%s image=%s",
                 entry.action, self.project_name,
                 entry.container_id[:12] if entry.container_id else "-",
                 entry.image or "-")
        await self._extend_rtmr(entry)

    async def _extend_rtmr(self, entry: AuditEntry):
        """Extend RTMR with audit entry for attestation."""
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
        """Export audit log as JSON."""
        entries = self.manager._load_entries(self.project_name)
        return [asdict(e) for e in entries]

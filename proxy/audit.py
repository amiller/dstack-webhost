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
    prev_hash: str = ""
    entry_hash: str = ""


class AuditLogManager:
    """Per-project audit log manager with disk persistence."""

    def __init__(self, audit_dir: str, dstack_socket: str | None = None):
        self.audit_dir = audit_dir
        self.dstack_socket = dstack_socket
        self.replayed = False
        os.makedirs(audit_dir, exist_ok=True)

    def _audit_file(self, project_name: str) -> str:
        return os.path.join(self.audit_dir, f"{project_name}.jsonl")

    def _head_file(self, project_name: str) -> str:
        return os.path.join(self.audit_dir, f"{project_name}.head")

    def _load_entries(self, project_name: str) -> list[AuditEntry]:
        """Load audit entries from disk for a project.

        Entries written before #61's chain existed carry no hashes at all. They are
        unverifiable by construction, so a LEADING run of them is adopted: each is
        anchored by its computed hash so later entries chain onto it, and the adoption
        is logged. Without this, any daemon whose audit dir predates #61 refuses to
        boot — which is how webhost-staging went down on 2026-08-24 (`attested-demo`).

        Adoption does not open a hole in a ledger that has one: blanking a later
        entry's hashes only reconstructs the same value, and editing its content
        changes that value, so the `.head` and prev_hash checks below still catch it.
        A project whose file is legacy end-to-end has no `.head` and had no integrity
        to lose — it gains one from its next entry on.
        """
        entries = []
        legacy = 0
        audit_file = self._audit_file(project_name)
        if os.path.exists(audit_file):
            with open(audit_file, "r") as f:
                for line in f:
                    if line.strip():
                        entry = AuditEntry(**json.loads(line))
                        expected = self._entry_hash(entry)
                        if not entry.entry_hash and not entry.prev_hash and legacy == len(entries):
                            entry.prev_hash = entries[-1].entry_hash if entries else ""
                            entry.entry_hash = self._entry_hash(entry)
                            legacy += 1
                            entries.append(entry)
                            continue
                        if entry.prev_hash != (entries[-1].entry_hash if entries else ""):
                            raise ValueError(f"Audit chain broken for {project_name}")
                        if entry.entry_hash != expected:
                            raise ValueError(f"Audit entry tampered for {project_name}")
                        entries.append(entry)
        if legacy:
            log.info("audit %s: adopted %d pre-ledger entries (no hash on disk)",
                     project_name, legacy)
        head_file = self._head_file(project_name)
        if os.path.exists(head_file):
            with open(head_file) as f:
                head = f.read().strip()
            if head != (entries[-1].entry_hash if entries else ""):
                raise ValueError(f"Audit ledger truncated for {project_name}")
        return entries

    @staticmethod
    def _entry_hash(entry: AuditEntry) -> str:
        data = asdict(entry)
        data["entry_hash"] = ""
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _save_entry(self, project_name: str, entry: AuditEntry) -> AuditEntry:
        """Append an audit entry to disk."""
        entries = self._load_entries(project_name)
        entry.prev_hash = entries[-1].entry_hash if entries else ""
        entry.entry_hash = self._entry_hash(entry)
        audit_file = self._audit_file(project_name)
        with open(audit_file, "a") as f:
            f.write(json.dumps(asdict(entry)) + "\n")
        with open(self._head_file(project_name), "w") as f:
            f.write(entry.entry_hash + "\n")
        return entry

    def set_dstack_socket(self, dstack_socket: str | None):
        self.dstack_socket = dstack_socket

    async def replay_anchors(self):
        if not self.dstack_socket:
            return
        for project in sorted(name[:-6] for name in os.listdir(self.audit_dir) if name.endswith(".jsonl")):
            for entry in self._load_entries(project):
                await AuditLog(project, self)._extend_rtmr(entry)
        self.replayed = True

    def get_audit_log(self, project_name: str) -> "AuditLog":
        """Get an AuditLog instance for a specific project."""
        return AuditLog(project_name, self)

    def delete_audit_log(self, project_name: str):
        """Delete audit log for a project."""
        audit_file = self._audit_file(project_name)
        if os.path.exists(audit_file):
            os.remove(audit_file)
        head_file = self._head_file(project_name)
        if os.path.exists(head_file):
            os.remove(head_file)


class AuditLog:
    """Project-specific audit log with RTMR measurement extension."""

    def __init__(self, project_name: str, manager: AuditLogManager,
                 dstack_socket: str | None = None):
        self.project_name = project_name
        self.manager = manager
        self.dstack_socket = dstack_socket

    async def record(self, entry: AuditEntry):
        """Record an audit entry and extend RTMR if configured."""
        entry = self.manager._save_entry(self.project_name, entry)
        log.info("AUDIT %s project=%s container=%s image=%s",
                 entry.action, self.project_name,
                 entry.container_id[:12] if entry.container_id else "-",
                 entry.image or "-")
        await self._extend_rtmr(entry)

    async def _extend_rtmr(self, entry: AuditEntry):
        """Extend RTMR with audit entry for attestation."""
        dstack_socket = self.dstack_socket or self.manager.dstack_socket
        if not dstack_socket:
            return
        payload = json.dumps(asdict(entry), sort_keys=True)
        payload_hex = payload.encode().hex()
        body = {"event": f"tee-proxy:{entry.action}", "payload": payload_hex}
        conn = aiohttp.UnixConnector(path=dstack_socket)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.post("http://localhost/EmitEvent", json=body) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"EmitEvent returned {resp.status}")
                log.info("RTMR extended: %s", entry.action)

    def to_json(self) -> list[dict]:
        """Export audit log as JSON."""
        entries = self.manager._load_entries(self.project_name)
        return [asdict(e) for e in entries]

    def history(self, project: object) -> dict:
        versions = []
        for entry in self.manager._load_entries(self.project_name):
            if entry.action not in ("deploy", "promote", "unpromote"):
                continue
            detail = json.loads(entry.detail) if entry.detail else {}
            versions.append({
                "sequence": len(versions),
                "timestamp": entry.timestamp,
                "action": entry.action,
                "mode": detail.get("to_mode", detail.get("mode", project.mode)),
                "source": detail.get("source", ""),
                "ref": detail.get("ref", ""),
                "commit_sha": detail.get("commit", ""),
                "tree_hash": detail.get("tree_hash", ""),
                "image": detail.get("image", entry.image),
                "image_digest": detail.get("image_digest", entry.image_digest),
                "current": (
                    detail.get("commit", "") == project.commit_sha
                    and detail.get("tree_hash", "") == project.tree_hash
                    and detail.get("image_digest", entry.image_digest) == project.image_digest
                    and detail.get("to_mode", detail.get("mode", project.mode)) == project.mode
                ),
                "entry_hash": entry.entry_hash,
            })
        return {
            "project": self.project_name,
            "tamper_evident": True,
            "anchor_status": "rtmr" if self.manager.dstack_socket else "unavailable",
            "attestation_replayed": self.manager.replayed,
            "versions": versions,
        }

"""Temporary tunnel management for ephemeral proxied access."""

import json
import secrets
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from typing import Optional
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

MAX_TIMEOUT = 86400  # 24 hours in seconds
DEFAULT_TIMEOUT = 3600  # 1 hour in seconds


@dataclass
class Tunnel:
    id: str
    tid: str  # 256-bit secret for tunnel identification (not to be shared with visitors)
    backend: str
    timeout: int
    created_at: str
    expires_at: str
    auth_mode: str = "none"

    def is_expired(self) -> bool:
        """Check if the tunnel has expired."""
        try:
            expires = datetime.fromisoformat(self.expires_at.replace('Z', '+00:00'))
            return datetime.now(timezone.utc) >= expires
        except Exception:
            return True


@dataclass
class TunnelResponse:
    id: str
    url: str
    expires_at: str
    tid: str


class TunnelStore:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self._tunnels: dict[str, Tunnel] = {}
        self._cleanup_interval = 60  # Check for expired tunnels every 60 seconds
        os.makedirs(base_dir, exist_ok=True)

    def _tunnel_path(self, tunnel_id: str) -> str:
        return os.path.join(self.base_dir, f"{tunnel_id}.json")

    def create(self, backend: str, timeout: int, auth_mode: str = "none") -> Tunnel:
        """Create a new tunnel."""
        # Validate timeout
        if timeout <= 0 or timeout > MAX_TIMEOUT:
            raise ValueError(f"Timeout must be between 1 and {MAX_TIMEOUT} seconds")

        # Validate auth mode
        if auth_mode not in ("none", "bearer"):
            raise ValueError(f"Invalid auth mode: {auth_mode}")

        # Generate tunnel ID (short, URL-safe)
        tunnel_id = f"t-{secrets.token_urlsafe(8)}"

        # Generate 256-bit tunnel identifier (tid) - not to be shared with visitors
        tid = secrets.token_hex(32)

        # Calculate expiration
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=timeout)

        tunnel = Tunnel(
            id=tunnel_id,
            tid=tid,
            backend=backend,
            timeout=timeout,
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
            auth_mode=auth_mode
        )

        self._tunnels[tunnel_id] = tunnel
        self._save_tunnel(tunnel)
        log.info("Created tunnel %s -> %s (expires at %s)", tunnel_id, backend, expires)
        return tunnel

    def get(self, tunnel_id: str) -> Optional[Tunnel]:
        """Get a tunnel by ID, checking for expiration."""
        tunnel = self._tunnels.get(tunnel_id)
        if tunnel:
            if tunnel.is_expired():
                self.delete(tunnel_id)
                return None
        return tunnel

    def get_by_tid(self, tid: str) -> Optional[Tunnel]:
        """Get a tunnel by its TID (256-bit secret)."""
        for tunnel in self._tunnels.values():
            if tunnel.tid == tid:
                if tunnel.is_expired():
                    self.delete(tunnel.id)
                    return None
                return tunnel
        return None

    def list(self) -> list[Tunnel]:
        """List all active (non-expired) tunnels."""
        now = datetime.now(timezone.utc)
        active = []
        expired_ids = []

        for tunnel_id, tunnel in self._tunnels.items():
            try:
                expires = datetime.fromisoformat(tunnel.expires_at.replace('Z', '+00:00'))
                if expires > now:
                    active.append(tunnel)
                else:
                    expired_ids.append(tunnel_id)
            except Exception:
                expired_ids.append(tunnel_id)

        # Remove expired tunnels
        for tunnel_id in expired_ids:
            self.delete(tunnel_id)

        return active

    def delete(self, tunnel_id: str) -> bool:
        """Delete a tunnel."""
        tunnel_path = self._tunnel_path(tunnel_id)
        if os.path.exists(tunnel_path):
            os.unlink(tunnel_path)
        if tunnel_id in self._tunnels:
            del self._tunnels[tunnel_id]
            log.info("Deleted tunnel %s", tunnel_id)
            return True
        return False

    def cleanup_expired(self):
        """Remove all expired tunnels."""
        expired = [tid for tid, tunnel in self._tunnels.items() if tunnel.is_expired()]
        for tunnel_id in expired:
            self.delete(tunnel_id)
        if expired:
            log.info("Cleaned up %d expired tunnels", len(expired))

    def _save_tunnel(self, tunnel: Tunnel):
        """Save tunnel to disk for persistence."""
        tunnel_path = self._tunnel_path(tunnel.id)
        with open(tunnel_path, "w") as f:
            json.dump(asdict(tunnel), f, indent=2)

    def recover(self):
        """Recover tunnels from disk on startup."""
        if not os.path.exists(self.base_dir):
            return

        for fname in os.listdir(self.base_dir):
            if not fname.endswith('.json'):
                continue

            tunnel_id = fname[:-5]  # Remove .json
            tunnel_path = os.path.join(self.base_dir, fname)

            try:
                with open(tunnel_path, "r") as f:
                    data = json.load(f)
                tunnel = Tunnel(**data)

                # Skip expired tunnels
                if tunnel.is_expired():
                    os.unlink(tunnel_path)
                    continue

                self._tunnels[tunnel_id] = tunnel
                log.info("Recovered tunnel %s (expires at %s)", tunnel_id, tunnel.expires_at)
            except Exception as e:
                log.warning("Failed to recover tunnel %s: %s", tunnel_id, e)
                if os.path.exists(tunnel_path):
                    os.unlink(tunnel_path)

"""Credential broker — sealed grant store with proxy mode delegation.

Holds upstream secrets sealed under a dstack-derived key, offers scoped
expiring delegations via proxy mode (secret never reaches handler), and logs
every use. Mirrors tunnel.py's structural model (TTL, JSON-file-per-id store,
recover()) with AEAD sealing added.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, asdict, field
from typing import Optional
from datetime import datetime, timezone, timedelta

import aiohttp
from aiohttp import web

log = logging.getLogger(__name__)


# Path prefix for dstack GetKey calls
KEY_PATH_PREFIX = "/tee-daemon/broker/seal"
# Key derivation info for HKDF
SEAL_KEY_INFO = b"tee-daemon/broker/seal/v1"


def _hkdf_sha256(ikm: bytes, salt: bytes = b"", info: bytes = b"", length: int = 32) -> bytes:
    """HKDF-SHA256 key derivation."""
    try:
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        raise RuntimeError("cryptography library required for broker sealing")

    kdf = HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt,
        info=info,
        backend=default_backend()
    )
    return kdf.derive(ikm)


def _seal_secret(secret: str, grant_id: str, seal_key: bytes) -> dict:
    """Seal a secret with AES-256-GCM. Returns dict with nonce and ciphertext."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise RuntimeError("cryptography library required for broker sealing")

    nonce = os.urandom(12)
    aead = AESGCM(seal_key)
    ct = aead.encrypt(nonce, secret.encode("utf-8"), aad=grant_id.encode("utf-8"))
    return {"nonce": nonce.hex(), "ct": ct.hex()}


def _unseal_secret(sealed: dict, grant_id: str, seal_key: bytes) -> str:
    """Unseal a secret with AES-256-GCM."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise RuntimeError("cryptography library required for broker sealing")

    nonce = bytes.fromhex(sealed["nonce"])
    ct = bytes.fromhex(sealed["ct"])
    aead = AESGCM(seal_key)
    pt = aead.decrypt(nonce, ct, aad=grant_id.encode("utf-8"))
    return pt.decode("utf-8")


async def _derive_seal_key(dstack_sock: str) -> bytes:
    """Derive the sealing key from dstack GetKey."""
    body = {"path": KEY_PATH_PREFIX}
    conn = aiohttp.UnixConnector(path=dstack_sock)
    async with aiohttp.ClientSession(connector=conn) as session:
        async with session.post("http://localhost/GetKey", json=body) as resp:
            if resp.status != 200:
                raise RuntimeError(f"GetKey failed: {resp.status}")
            data = await resp.json()
            # GetKey returns the private key as hex; we use the first 32 bytes
            key_hex = data.get("key", "").replace("0x", "")
            if not key_hex:
                raise RuntimeError("GetKey returned no key")
            ikm = bytes.fromhex(key_hex)[:32]
    return _hkdf_sha256(ikm, salt=b"", info=SEAL_KEY_INFO, length=32)


@dataclass
class Grant:
    """A sealed credential grant."""
    id: str
    project: str
    name: str
    mode: str  # "proxy" | "issue" (MVP is proxy only)
    scope: str
    upstream: dict  # {base_url, allow_paths, allow_methods, inject}
    sealed: dict  # {nonce, ct} — ciphertext
    created_at: str
    expires_at: Optional[str] = None  # null = no expiry
    last_used_at: Optional[str] = None
    revoked: bool = False
    require_approval: bool = False  # reserved for future

    def is_expired(self) -> bool:
        """Check if the grant has expired."""
        if not self.expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.expires_at.replace('Z', '+00:00'))
            return datetime.now(timezone.utc) >= expires
        except Exception:
            return True

    def to_json(self) -> dict:
        """Serialize to JSON, omitting sealed (never exposed)."""
        data = asdict(self)
        data.pop("sealed", None)
        # Convert None to empty strings for last_used_at
        if data.get("last_used_at") is None:
            data["last_used_at"] = ""
        return data


class BrokerStore:
    """Sealed credential store with proxy mode delegation."""

    def __init__(self, base_dir: str, creds_dir: str, dstack_sock: Optional[str] = None):
        self.base_dir = base_dir
        self.creds_dir = creds_dir
        self.dstack_sock = dstack_sock
        self._grants: dict[str, Grant] = {}
        self._seal_key: Optional[bytes] = None
        os.makedirs(base_dir, exist_ok=True)
        os.makedirs(creds_dir, exist_ok=True)

    async def _ensure_seal_key(self):
        """Lazy-derive the sealing key on first use."""
        if self._seal_key is not None:
            return
        if not self.dstack_sock:
            raise RuntimeError("dstack not available — cannot seal grants")
        try:
            self._seal_key = await _derive_seal_key(self.dstack_sock)
            log.info("Derived sealing key from dstack")
        except Exception as e:
            log.error("Failed to derive sealing key: %s", e)
            raise RuntimeError("dstack not available — cannot seal grants") from e

    def _grant_path(self, grant_id: str) -> str:
        return os.path.join(self.base_dir, f"{grant_id}.json")

    def _usage_path(self, project: str) -> str:
        return os.path.join(self.creds_dir, f"{project}.jsonl")

    def _append_usage(self, project: str, entry: dict):
        """Append a usage record to the project's jsonl file."""
        path = self._usage_path(project)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def create(self, project: str, name: str, scope: str,
                     upstream: dict, secret: str, ttl: Optional[int] = None,
                     mode: str = "proxy") -> Grant:
        """Create a new grant. Secret is sealed and never returned."""
        await self._ensure_seal_key()

        if mode not in ("proxy", "issue"):
            raise ValueError(f"Invalid mode: {mode}")

        # Generate grant ID
        grant_id = f"g-{secrets.token_urlsafe(8)}"

        # Calculate expiration
        now = datetime.now(timezone.utc)
        expires_at = None
        if ttl:
            expires = now + timedelta(seconds=ttl)
            expires_at = expires.isoformat()

        # Seal the secret
        sealed = _seal_secret(secret, grant_id, self._seal_key)

        grant = Grant(
            id=grant_id,
            project=project,
            name=name,
            mode=mode,
            scope=scope,
            upstream=upstream,
            sealed=sealed,
            created_at=now.isoformat(),
            expires_at=expires_at,
            last_used_at=None,
            revoked=False,
            require_approval=False
        )

        self._grants[grant_id] = grant
        self._save_grant(grant)
        log.info("Created grant %s for project %s (expires: %s)", grant_id, project, expires_at)
        return grant

    def get(self, grant_id: str) -> Optional[Grant]:
        """Get a grant by ID, checking expiration."""
        grant = self._grants.get(grant_id)
        if grant:
            if grant.is_expired():
                self.delete(grant_id)
                return None
            if grant.revoked:
                return None
        return grant

    def list(self, project: Optional[str] = None) -> list[Grant]:
        """List active grants, optionally filtered by project."""
        active = []
        expired_ids = []

        for grant_id, grant in self._grants.items():
            if project and grant.project != project:
                continue
            if grant.is_expired() or grant.revoked:
                expired_ids.append(grant_id)
                continue
            active.append(grant)

        # Clean up expired
        for gid in expired_ids:
            self.delete(gid)

        return active

    def delete(self, grant_id: str) -> bool:
        """Delete a grant (revoke)."""
        grant_path = self._grant_path(grant_id)
        if os.path.exists(grant_path):
            os.unlink(grant_path)
        if grant_id in self._grants:
            del self._grants[grant_id]
            log.info("Deleted grant %s", grant_id)
            return True
        return False

    def revoke(self, grant_id: str) -> bool:
        """Revoke a grant (immediate effect)."""
        grant = self._grants.get(grant_id)
        if not grant:
            return False
        grant.revoked = True
        # Delete the sealed file so it's gone from disk
        self.delete(grant_id)
        log.info("Revoked grant %s", grant_id)
        return True

    async def reauthorize(self, grant_id: str, secret: Optional[str] = None,
                          ttl: Optional[int] = None, scope: Optional[str] = None) -> Optional[Grant]:
        """Reauthorize a grant: rotate secret, extend TTL, or change scope."""
        await self._ensure_seal_key()

        grant = self._grants.get(grant_id)
        if not grant:
            return None

        # Rotate secret if provided
        if secret is not None:
            sealed = _seal_secret(secret, grant_id, self._seal_key)
            grant.sealed = sealed

        # Update TTL
        if ttl is not None:
            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=ttl)
            grant.expires_at = expires.isoformat()

        # Update scope
        if scope is not None:
            grant.scope = scope

        self._save_grant(grant)
        log.info("Reauthorized grant %s", grant_id)
        return grant

    def get_usage(self, grant_id: str, limit: int = 100) -> list[dict]:
        """Get recent usage for a grant."""
        grant = self._grants.get(grant_id)
        if not grant:
            return []

        path = self._usage_path(grant.project)
        if not os.path.exists(path):
            return []

        entries = []
        with open(path, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("grant_id") == grant_id:
                        entries.append(entry)
                        if len(entries) >= limit:
                            break
                except json.JSONDecodeError:
                    continue

        # Return most recent first
        return list(reversed(entries))

    def cleanup_expired(self):
        """Remove all expired grants."""
        expired = [gid for gid, grant in self._grants.items() if grant.is_expired()]
        for grant_id in expired:
            self.delete(grant_id)
        if expired:
            log.info("Cleaned up %d expired grants", len(expired))

    def _save_grant(self, grant: Grant):
        """Save grant to disk (sealed only)."""
        grant_path = self._grant_path(grant.id)
        with open(grant_path, "w") as f:
            json.dump(asdict(grant), f, indent=2)

    async def recover(self):
        """Recover grants from disk on startup."""
        await self._ensure_seal_key()

        if not os.path.exists(self.base_dir):
            return

        for fname in os.listdir(self.base_dir):
            if not fname.endswith('.json'):
                continue

            grant_id = fname[:-5]
            grant_path = os.path.join(self.base_dir, fname)

            try:
                with open(grant_path, "r") as f:
                    data = json.load(f)
                grant = Grant(**data)

                # Skip expired/revoked grants
                if grant.is_expired() or grant.revoked:
                    os.unlink(grant_path)
                    continue

                self._grants[grant_id] = grant
                log.info("Recovered grant %s for project %s", grant_id, grant.project)
            except Exception as e:
                log.warning("Failed to recover grant %s: %s", grant_id, e)
                if os.path.exists(grant_path):
                    os.unlink(grant_path)


class BrokerProxy:
    """Handler for /run/broker/creds.sock — proxy mode delegation."""

    def __init__(self, broker_store: BrokerStore, runtime_manager=None):
        self.broker_store = broker_store
        self.runtime_manager = runtime_manager  # For caller token auth

    async def handle(self, request: web.Request) -> web.Response:
        """Handle a proxy request: <METHOD> /proxy/<grant-id><subpath>"""
        path = request.path.lstrip("/")

        # Parse: /proxy/<grant-id><subpath>
        if not path.startswith("proxy/"):
            return web.json_response({"error": "invalid path"}, status=400)

        rest = path[6:]  # After "proxy/"
        if not rest:
            return web.json_response({"error": "grant id required"}, status=400)

        # Split grant-id from subpath
        # grant-id is like "g-AbC12xYz", subpath starts with "/"
        parts = rest.split("/", 1)
        grant_id = parts[0] if parts[0] else ""
        subpath = "/" + parts[1] if len(parts) > 1 else "/"

        if not grant_id:
            return web.json_response({"error": "grant id required"}, status=400)

        # Load grant
        grant = self.broker_store.get(grant_id)
        if not grant:
            return web.json_response({"error": "grant not found or expired"}, status=404)

        # Check require_approval (MVP: deny if set)
        if grant.require_approval:
            return web.json_response({"error": "grant requires approval"}, status=403)

        # Authenticate caller via X-Broker-Token header
        auth_header = request.headers.get("X-Broker-Token", "")
        if not auth_header:
            return web.json_response({"error": "missing broker token"}, status=401)

        # Verify caller token matches grant.project
        caller_project = self._verify_broker_token(auth_header)
        if not caller_project:
            return web.json_response({"error": "invalid broker token"}, status=401)
        if caller_project != grant.project:
            return web.json_response({"error": "token project mismatch"}, status=403)

        # Enforce pin: check method
        allow_methods = grant.upstream.get("allow_methods", ["POST"])
        if request.method not in allow_methods:
            self._log_usage(grant, request.method, subpath, "denied-method")
            return web.json_response({"error": "method not allowed"}, status=403)

        # Enforce pin: check path prefix
        allow_paths = grant.upstream.get("allow_paths", ["/*"])
        path_allowed = False
        for allowed in allow_paths:
            # Simple prefix match; "/*" matches everything
            if allowed == "/*" or subpath.startswith(allowed.rstrip("*")):
                path_allowed = True
                break
        if not path_allowed:
            self._log_usage(grant, request.method, subpath, "denied-path")
            return web.json_response({"error": "path not allowed"}, status=403)

        # Build upstream request
        base_url = grant.upstream.get("base_url", "")
        if not base_url:
            return web.json_response({"error": "grant missing base_url"}, status=500)

        url = f"{base_url}{subpath}"
        qs = request.query_string
        if qs:
            url += f"?{qs}"

        # Unseal secret
        try:
            await self.broker_store._ensure_seal_key()
            secret = _unseal_secret(grant.sealed, grant_id, self.broker_store._seal_key)
        except Exception as e:
            log.error("Failed to unseal secret for grant %s: %s", grant_id, e)
            return web.json_response({"error": "internal error"}, status=500)

        # Prepare headers: copy from request, inject credential
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "transfer-encoding", "accept-encoding")}

        # Inject credential per upstream config
        inject = grant.upstream.get("inject", {})
        if inject.get("header"):
            header_name = inject["header"]
            template = inject.get("template", "Bearer {secret}")
            headers[header_name] = template.replace("{secret}", secret)

        # Make upstream request
        body = await request.read()
        outcome = "error"
        status_code = 500
        resp_body = b""
        resp_headers = {}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(request.method, url,
                                           data=body if body else None,
                                           headers=headers) as resp:
                    status_code = resp.status
                    resp_headers = {k: v for k, v in resp.headers.items()
                                    if k.lower() in ("content-type",)}
                    resp_body = await resp.read()
                    outcome = str(status_code)
        except Exception as e:
            log.error("Upstream request failed for grant %s: %s", grant_id, e)
            outcome = "error"

        # Update last_used_at
        grant.last_used_at = datetime.now(timezone.utc).isoformat()
        self.broker_store._save_grant(grant)

        # Log usage
        self._log_usage(grant, request.method, subpath, outcome)

        # Return response
        return web.Response(body=resp_body, status=status_code,
                           content_type=resp_headers.get("content-type"))

    def _verify_broker_token(self, token: str) -> Optional[str]:
        """Verify a broker token and return the associated project name."""
        if not self.runtime_manager:
            return None
        return self.runtime_manager.get_broker_project(token)

    def _log_usage(self, grant: Grant, method: str, subpath: str, outcome: str):
        """Log a usage event."""
        entry = {
            "ts": datetime.now(timezone.utc).timestamp(),
            "project": grant.project,
            "grant_id": grant.id,
            "mode": grant.mode,
            "scope": grant.scope,
            "upstream": grant.upstream.get("base_url", ""),
            "method": method,
            "subpath": subpath,
            "outcome": outcome
        }
        self.broker_store._append_usage(grant.project, entry)

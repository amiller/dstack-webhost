"""Scoped bearer token management for the daemon API."""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

MAX_TTL = 86400
DEFAULT_TTL = 3600
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
API_PREFIXES = {
    "audit",
    "attest",
    "projects",
    "routes",
    "substrate",
    "tunnels",
    "tokens",
    "verification",
}


@dataclass
class ApiToken:
    id: str
    scope: str
    ttl: int
    created_at: str
    expires_at: str
    revoked: bool
    secret_hash: str

    def is_expired(self) -> bool:
        try:
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) >= expires
        except Exception:
            return True

    def public(self) -> dict:
        data = asdict(self)
        data.pop("secret_hash", None)
        return data


class TokenStore:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self._tokens: dict[str, ApiToken] = {}
        os.makedirs(base_dir, exist_ok=True)

    def _token_path(self, token_id: str) -> str:
        return os.path.join(self.base_dir, f"{token_id}.json")

    def create(self, scope: str, ttl: int) -> tuple[ApiToken, str]:
        if ttl <= 0 or ttl > MAX_TTL:
            raise ValueError(f"TTL must be between 1 and {MAX_TTL} seconds")

        normalized_scope = normalize_scope(scope)
        if not normalized_scope:
            raise ValueError(f"Invalid scope: {scope!r}")

        token_id = f"tok-{secrets.token_urlsafe(8)}"
        secret = secrets.token_urlsafe(32)
        bearer = f"tdt_{token_id}_{secret}"
        now = datetime.now(timezone.utc)
        token = ApiToken(
            id=token_id,
            scope=normalized_scope,
            ttl=ttl,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl)).isoformat(),
            revoked=False,
            secret_hash=_hash_secret(bearer),
        )
        self._tokens[token_id] = token
        self._save_token(token)
        log.info("Created scoped API token %s scope=%s expires=%s",
                 token_id, normalized_scope, token.expires_at)
        return token, bearer

    def authenticate(self, bearer: str, api_path: str) -> Optional[ApiToken]:
        for token in list(self._tokens.values()):
            if token.is_expired():
                self.delete(token.id)
                continue
            if token.revoked:
                continue
            if hmac.compare_digest(token.secret_hash, _hash_secret(bearer)):
                return token if scope_allows(token.scope, api_path) else None
        return None

    def list(self) -> list[ApiToken]:
        active = []
        for token in list(self._tokens.values()):
            if token.is_expired():
                self.delete(token.id)
            else:
                active.append(token)
        return sorted(active, key=lambda t: t.created_at)

    def revoke(self, token_id: str) -> bool:
        token = self._tokens.get(token_id)
        if not token:
            return False
        token.revoked = True
        self._save_token(token)
        log.info("Revoked scoped API token %s", token_id)
        return True

    def delete(self, token_id: str) -> bool:
        token_path = self._token_path(token_id)
        if os.path.exists(token_path):
            os.unlink(token_path)
        if token_id in self._tokens:
            del self._tokens[token_id]
            return True
        return False

    def cleanup_expired(self):
        for token_id in [tid for tid, token in self._tokens.items() if token.is_expired()]:
            self.delete(token_id)

    def recover(self):
        if not os.path.exists(self.base_dir):
            return

        for fname in os.listdir(self.base_dir):
            if not fname.endswith(".json"):
                continue
            token_id = fname[:-5]
            token_path = os.path.join(self.base_dir, fname)
            try:
                with open(token_path, "r") as f:
                    data = json.load(f)
                token = ApiToken(**data)
                if token.is_expired():
                    os.unlink(token_path)
                    continue
                self._tokens[token_id] = token
                log.info("Recovered scoped API token %s", token_id)
            except Exception as e:
                log.warning("Failed to recover scoped API token %s: %s", token_id, e)
                if os.path.exists(token_path):
                    os.unlink(token_path)

    def _save_token(self, token: ApiToken):
        with open(self._token_path(token.id), "w") as f:
            json.dump(asdict(token), f, indent=2)


def normalize_scope(scope: str) -> str:
    if not isinstance(scope, str):
        return ""
    scope = scope.strip().strip("/")
    if not scope:
        return ""

    parts = scope.split("/")
    if len(parts) == 1 and NAME_RE.fullmatch(parts[0]) and parts[0] not in API_PREFIXES:
        return f"projects/{parts[0]}"
    if parts[0] not in API_PREFIXES:
        return ""
    if any(not part for part in parts):
        return ""
    return scope


def scope_allows(scope: str, api_path: str) -> bool:
    normalized = normalize_scope(scope)
    path = api_path.strip("/")
    if not normalized:
        return False
    return path == normalized or path.startswith(normalized + "/")


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()

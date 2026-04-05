"""In-memory allow-list of container IDs created through the proxy."""

import threading
from datetime import datetime, timezone


class ContainerTracker:
    def __init__(self):
        self._ids: dict[str, str] = {}  # full_id -> created_at ISO
        self._lock = threading.Lock()

    def add(self, container_id: str) -> None:
        with self._lock:
            self._ids[container_id] = datetime.now(timezone.utc).isoformat()

    def remove(self, container_id: str) -> None:
        full = self._resolve(container_id)
        if full:
            with self._lock:
                self._ids.pop(full, None)

    def is_allowed(self, container_id: str) -> bool:
        return self._resolve(container_id) is not None

    def full_id(self, container_id: str) -> str | None:
        return self._resolve(container_id)

    def all_ids(self) -> set[str]:
        with self._lock:
            return set(self._ids)

    def _resolve(self, cid: str) -> str | None:
        with self._lock:
            if cid in self._ids:
                return cid
            for full in self._ids:
                if full.startswith(cid):
                    return full
        return None

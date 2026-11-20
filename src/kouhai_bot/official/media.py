"""file_info cache keyed by image content hash (ttl-aware reuse).

Official media upload returns a ``file_info`` with a ``ttl``; the same
file_info can be reused (group-uploaded files send to groups only). Caching
by content hash avoids re-uploading identical cards (e.g. /problem resend).
"""

from __future__ import annotations

import hashlib
import time

_TTL_MARGIN_SEC = 30.0


def content_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MediaCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        file_info, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._store[key]
            return None
        return file_info

    def put(self, key: str, file_info: str, ttl_sec: float) -> None:
        expires_at = time.monotonic() + max(0.0, ttl_sec - _TTL_MARGIN_SEC)
        self._store[key] = (file_info, expires_at)
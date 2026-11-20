"""Fetch official avatar URLs into embeddable data URIs for card rendering.

Official group events deliver ``author.avatar`` (实测: a public
``thirdqq.qlogo.cn`` JPEG, no auth). We pre-fetch in Python and embed a base64
data URI so the headless render needs no network and cannot stall on a slow
avatar host. Any failure degrades to the letter-color circle fallback.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections import OrderedDict

import aiohttp

logger = logging.getLogger("kouhai-bot.render.avatars")

_FETCH_TIMEOUT_SEC = 5.0
_MAX_BYTES = 2 * 1024 * 1024
_CACHE_TTL_SEC = 6 * 3600
_FAILURE_TTL_SEC = 60.0
_CACHE_MAX = 512


class AvatarCache:
    def __init__(self) -> None:
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._failures: dict[str, float] = {}
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SEC)
            )
        return self._session

    async def _note_failure(self, url: str) -> None:
        async with self._lock:
            now = time.monotonic()
            self._failures[url] = now + _FAILURE_TTL_SEC
            if len(self._failures) > _CACHE_MAX:
                for key in list(self._failures):
                    if self._failures[key] <= now:
                        del self._failures[key]

    async def data_uri(self, url: str) -> str | None:
        if not url or not url.startswith(("http://", "https://")):
            return None
        now = time.monotonic()
        cached = self._cache.get(url)
        if cached and cached[1] > now:
            self._cache.move_to_end(url)
            return cached[0]
        # Negative cache: a dead/slow URL fails once, then returns instantly for
        # a short TTL instead of stalling every render for the full timeout.
        if self._failures.get(url, 0.0) > now:
            return None
        # Fetch outside the lock so distinct avatars download concurrently.
        try:
            async with self._lock:
                session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    await self._note_failure(url)
                    return None
                data = await resp.read()
                if not data or len(data) > _MAX_BYTES:
                    await self._note_failure(url)
                    return None
                ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
                if not ctype.startswith("image/"):
                    ctype = "image/jpeg"
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.debug("avatar fetch failed for %s: %s", url[:48], exc)
            await self._note_failure(url)
            return None
        uri = f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}"
        async with self._lock:
            self._cache[url] = (uri, time.monotonic() + _CACHE_TTL_SEC)
            self._failures.pop(url, None)
            while len(self._cache) > _CACHE_MAX:
                self._cache.popitem(last=False)
        return uri

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


_cache: AvatarCache | None = None


def get_avatar_cache() -> AvatarCache:
    global _cache
    if _cache is None:
        _cache = AvatarCache()
    return _cache


def reset_avatar_cache_for_tests() -> None:
    global _cache
    _cache = None
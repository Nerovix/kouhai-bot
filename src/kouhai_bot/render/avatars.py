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
_CACHE_MAX = 512


class AvatarCache:
    def __init__(self) -> None:
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SEC)
            )
        return self._session

    async def data_uri(self, url: str) -> str | None:
        if not url or not url.startswith(("http://", "https://")):
            return None
        now = time.monotonic()
        cached = self._cache.get(url)
        if cached and cached[1] > now:
            self._cache.move_to_end(url)
            return cached[0]
        async with self._lock:
            cached = self._cache.get(url)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            try:
                session = await self._get_session()
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
                    if not data or len(data) > _MAX_BYTES:
                        return None
                    ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
                    if not ctype.startswith("image/"):
                        ctype = "image/jpeg"
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                logger.debug("avatar fetch failed for %s: %s", url[:48], exc)
                return None
            uri = f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}"
            self._cache[url] = (uri, time.monotonic() + _CACHE_TTL_SEC)
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
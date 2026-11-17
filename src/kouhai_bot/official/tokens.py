"""Official QQ bot access-token manager.

POST {token_base}/app/getAppAccessToken {appId, clientSecret} →
{access_token, expires_in}. Tokens are cached and refreshed shortly before
expiry; a lock prevents concurrent refresh storms.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

logger = logging.getLogger("kouhai-bot.official.tokens")

_REFRESH_MARGIN_SEC = 120.0


class AccessTokenManager:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        token_base: str = "https://api.bot.qq.com",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token_base = token_base.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            await self._refresh()
            return self._token

    async def _refresh(self) -> None:
        session = await self._get_session()
        url = f"{self._token_base}/app/getAppAccessToken"
        async with session.post(
            url, json={"appId": self._app_id, "clientSecret": self._app_secret}
        ) as resp:
            data = await resp.json()
        token = str(data.get("access_token", "") or "")
        if not token:
            raise RuntimeError(f"getAppAccessToken returned no token: {data}")
        expires_in = float(data.get("expires_in", 7200) or 7200)
        self._token = token
        self._expires_at = time.monotonic() + max(0.0, expires_in - _REFRESH_MARGIN_SEC)
        logger.info("official access token refreshed (expires_in=%ss)", expires_in)

    def invalidate(self) -> None:
        self._token = ""
        self._expires_at = 0.0

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
"""Official QQ bot REST client (api.bot.qq.com v2).

Payload-agnostic: the sender builds message payloads (text / markdown /
media); this layer adds ``Authorization: QQBot <token>`` and normalizes
errors. Since 2026-08-10 all official endpoints unify on ``api.bot.qq.com``
(the legacy ``api.sgroup.qq.com`` still aliases it).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .tokens import AccessTokenManager

logger = logging.getLogger("kouhai-bot.official.rest")


class OfficialApiError(RuntimeError):
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        super().__init__(f"official API error (HTTP {status}): {body}")


class OfficialRestClient:
    def __init__(
        self,
        tokens: AccessTokenManager,
        *,
        api_base: str = "https://api.bot.qq.com",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._tokens = tokens
        self._api_base = api_base.rstrip("/")
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            )
        return self._session

    async def _request(
        self, method: str, path: str, *, json: dict | None = None
    ) -> dict[str, Any]:
        token = await self._tokens.get_token()
        session = await self._get_session()
        headers = {"Authorization": f"QQBot {token}"}
        url = f"{self._api_base}{path}"
        try:
            async with session.request(method, url, json=json, headers=headers) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {"raw": await resp.text()}
                if resp.status != 200:
                    # A 401/403 may mean an expired token; invalidate for next call.
                    if resp.status in {401, 403}:
                        self._tokens.invalidate()
                    raise OfficialApiError(resp.status, body)
                if isinstance(body, dict) and body.get("code") not in (None, 0):
                    raise OfficialApiError(resp.status, body)
                return body if isinstance(body, dict) else {"data": body}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Normalize transport failures so callers (sender) degrade uniformly
            # instead of letting a network blip escape past their OfficialApiError
            # handling and abort the whole command.
            raise OfficialApiError(0, {"error": f"{type(exc).__name__}: {exc}"}) from exc

    async def get_gateway(self) -> str:
        data = await self._request("GET", "/gateway")
        url = str(data.get("url", "") or "")
        if not url:
            raise OfficialApiError(200, data)
        return url

    async def send_group_message(
        self, group_openid: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "POST", f"/v2/groups/{group_openid}/messages", json=payload
        )

    async def upload_group_media(
        self, group_openid: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "POST", f"/v2/groups/{group_openid}/files", json=payload
        )

    async def recall_group_message(
        self, group_openid: str, message_id: str
    ) -> bool:
        try:
            await self._request(
                "DELETE", f"/v2/groups/{group_openid}/messages/{message_id}"
            )
            return True
        except OfficialApiError as e:
            logger.warning("recall failed for %s: %s", message_id, e)
            return False

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
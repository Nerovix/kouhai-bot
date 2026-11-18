"""High-level group send API (replaces napcat.client send surface).

One official request carries exactly one content type (msg_type 0/2/7 are
mutually exclusive). Passive replies ride an incoming msg_id (5min/5);
when exhausted, fall back to active quota. @ single user only renders in
markdown (``<qqbot-at-user id="member_openid"/>``).
"""

from __future__ import annotations

import base64
import logging
import random
from collections import OrderedDict
from typing import Any

from .budget import ReplyBudget
from .media import MediaCache, content_key
from .rest import OfficialApiError, OfficialRestClient

logger = logging.getLogger("kouhai-bot.official.sender")

MSG_TEXT = 0
MSG_MARKDOWN = 2
MSG_MEDIA = 7


def build_text_payload(text: str) -> dict[str, Any]:
    return {"content": text, "msg_type": MSG_TEXT}


def build_markdown_payload(
    markdown: str, *, keyboard: dict | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "msg_type": MSG_MARKDOWN,
        "markdown": {"content": markdown},
    }
    if keyboard:
        payload["keyboard"] = keyboard
    return payload


def build_media_payload(file_info: str, *, content: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "msg_type": MSG_MEDIA,
        "media": {"file_info": file_info},
    }
    if content:
        payload["content"] = content
    return payload


def at_user_tag(user_openid: str) -> str:
    return f'<qqbot-at-user id="{user_openid}"/>'


def at_everyone_tag() -> str:
    return "<qqbot-at-everyone/>"


class OfficialSender:
    def __init__(
        self,
        rest: OfficialRestClient,
        budget: ReplyBudget,
        *,
        bot_name: str = "",
        media_cache: MediaCache | None = None,
    ) -> None:
        self._rest = rest
        self._budget = budget
        self.bot_name = bot_name
        self._media_cache = media_cache if media_cache is not None else MediaCache()
        # message_id → outbound ext_info.ref_idx (for quoted-card targeting).
        self._ref_by_id: OrderedDict[str, str] = OrderedDict()

    def _remember(self, resp: dict[str, Any]) -> str | None:
        mid = str(resp.get("id", "") or "") or None
        ref = str((resp.get("ext_info") or {}).get("ref_idx", "") or "")
        if mid and ref:
            self._ref_by_id[mid] = ref
            while len(self._ref_by_id) > 4096:
                self._ref_by_id.popitem(last=False)
        return mid

    def ref_idx_for(self, message_id: str | None) -> str | None:
        """The ref_idx returned when this message was sent (None if unknown)."""
        if not message_id:
            return None
        return self._ref_by_id.get(message_id)

    async def _send(
        self,
        group_openid: str,
        payload: dict[str, Any],
        *,
        msg_id: str | None = None,
    ) -> str | None:
        if msg_id:
            seq = self._budget.next_passive_seq(msg_id)
            if seq is not None:
                passive = dict(payload, msg_id=msg_id, msg_seq=seq)
                try:
                    resp = await self._rest.send_group_message(group_openid, passive)
                    return self._remember(resp)
                except OfficialApiError as e:
                    logger.warning(
                        "passive send failed (msg_id=%s seq=%s): %s; trying active",
                        msg_id, seq, e,
                    )
        if not await self._budget.acquire_active(group_openid):
            logger.warning("active reply quota exhausted for group %s", group_openid)
            return None
        active = dict(payload, msg_seq=random.randint(1, 1_000_000))
        try:
            resp = await self._rest.send_group_message(group_openid, active)
            return self._remember(resp)
        except OfficialApiError as e:
            logger.error("active send failed for group %s: %s", group_openid, e)
            return None

    async def send_group_text(
        self, group_openid: str, text: str, *, msg_id: str | None = None
    ) -> str | None:
        return await self._send(group_openid, build_text_payload(text), msg_id=msg_id)

    async def send_group_markdown(
        self,
        group_openid: str,
        markdown: str,
        *,
        msg_id: str | None = None,
        keyboard: dict | None = None,
    ) -> str | None:
        return await self._send(
            group_openid,
            build_markdown_payload(markdown, keyboard=keyboard),
            msg_id=msg_id,
        )

    async def send_group_at(
        self,
        group_openid: str,
        user_openid: str,
        text: str,
        *,
        msg_id: str | None = None,
    ) -> str | None:
        markdown = f"{at_user_tag(user_openid)} {text}"
        return await self.send_group_markdown(group_openid, markdown, msg_id=msg_id)

    async def send_group_media(
        self,
        group_openid: str,
        file_info: str,
        *,
        msg_id: str | None = None,
        content: str = "",
    ) -> str | None:
        return await self._send(
            group_openid, build_media_payload(file_info, content=content), msg_id=msg_id
        )

    async def send_group_image_url(
        self,
        group_openid: str,
        image_url: str,
        *,
        msg_id: str | None = None,
        content: str = "",
        file_type: int = 1,
    ) -> str | None:
        """Upload a public image URL, then send it as a media message."""
        try:
            up = await self._rest.upload_group_media(
                group_openid,
                {"file_type": file_type, "url": image_url, "srv_send_msg": False},
            )
        except OfficialApiError as e:
            logger.error("media upload failed for group %s: %s", group_openid, e)
            return None
        return await self._send_uploaded_media(
            group_openid, up, msg_id=msg_id, content=content
        )

    async def send_group_image_bytes(
        self,
        group_openid: str,
        image_bytes: bytes,
        *,
        msg_id: str | None = None,
        content: str = "",
        file_type: int = 1,
    ) -> str | None:
        """Upload locally rendered PNG bytes (base64), reusing cached file_info."""
        key = content_key(image_bytes)
        cached = self._media_cache.get(key)
        if cached:
            return await self.send_group_media(
                group_openid, cached, msg_id=msg_id, content=content
            )
        try:
            up = await self._rest.upload_group_media(
                group_openid,
                {
                    "file_type": file_type,
                    "file_data": base64.b64encode(image_bytes).decode("ascii"),
                    "srv_send_msg": False,
                },
            )
        except OfficialApiError as e:
            logger.error("base64 media upload failed for group %s: %s", group_openid, e)
            return None
        file_info = str(up.get("file_info", "") or "")
        if file_info:
            self._media_cache.put(key, file_info, float(up.get("ttl", 300) or 300))
        return await self._send_uploaded_media(
            group_openid, up, msg_id=msg_id, content=content
        )

    async def _send_uploaded_media(
        self,
        group_openid: str,
        up: dict[str, Any],
        *,
        msg_id: str | None,
        content: str,
    ) -> str | None:
        file_info = str(up.get("file_info", "") or "")
        if not file_info:
            logger.error("media upload returned no file_info: %s", up)
            return None
        return await self.send_group_media(
            group_openid, file_info, msg_id=msg_id, content=content
        )

    async def react(
        self, group_openid: str, emoji: str, *, msg_id: str | None = None
    ) -> str | None:
        """No official reaction API → a tiny emoji-character group message."""
        return await self.send_group_text(group_openid, emoji, msg_id=msg_id)

    async def recall(self, group_openid: str, message_id: str) -> bool:
        return await self._rest.recall_group_message(group_openid, message_id)
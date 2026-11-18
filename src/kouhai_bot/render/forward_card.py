"""Render a forward-card node list to a long image and send it.

Falls back to plain text when rendering or media upload fails (the official
API has no merged-forward primitive; the long image is the replica).
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import unescape
from typing import Any

from ..official.sender import OfficialSender
from .avatars import get_avatar_cache
from .browser import RenderBrowser, get_render_browser
from .html import build_forward_card_html

logger = logging.getLogger("kouhai-bot.render.card")

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def nodes_to_text(nodes: list[dict[str, Any]]) -> str:
    lines = []
    for node in nodes:
        nickname = str(node.get("nickname", "") or "")
        text = node.get("content_text")
        if text is None:
            text = _BR_RE.sub("\n", str(node.get("content_html", "")))
            text = unescape(_TAG_RE.sub("", text))
        lines.append(f"{nickname}：{text}".strip())
    return "\n\n".join(lines)


async def render_card_png(
    nodes: list[dict[str, Any]],
    *,
    title: str = "聊天记录",
    footer: str = "",
    width: int = 720,
    browser: RenderBrowser | None = None,
) -> bytes:
    html = build_forward_card_html(
        await _resolve_avatars(nodes), title=title, footer=footer
    )
    browser = browser or get_render_browser()
    return await browser.screenshot_html(html, width=width)


async def _resolve_avatars(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pre-fetch each node's avatar_url into an embedded data URI (best-effort;
    failures leave the letter-circle fallback). Fetches run concurrently so one
    slow avatar host does not head-of-line-block the whole card."""
    cache = get_avatar_cache()

    async def resolve(node: dict[str, Any]) -> dict[str, Any]:
        url = str(node.get("avatar_url", "") or "")
        if url and not node.get("avatar_data"):
            data = await cache.data_uri(url)
            if data:
                return {**node, "avatar_data": data}
        return node

    return list(await asyncio.gather(*(resolve(node) for node in nodes)))


async def send_forward_card(
    sender: OfficialSender,
    group_openid: str,
    nodes: list[dict[str, Any]],
    *,
    title: str = "聊天记录",
    footer: str = "",
    msg_id: str | None = None,
    text_fallback: str | None = None,
    browser: RenderBrowser | None = None,
) -> str | None:
    """Render → upload → send long image; degrade to text on any failure.

    Never raises: a render/upload/send error (including transport failures)
    falls back to plain text, and a failed text fallback returns None so the
    caller can report delivery failure instead of crashing mid-command.
    """
    png = None
    try:
        png = await render_card_png(
            nodes, title=title, footer=footer, browser=browser
        )
    except Exception as e:
        logger.warning("card render failed; using text fallback: %s", e)
    if png:
        try:
            mid = await sender.send_group_image_bytes(
                group_openid, png, msg_id=msg_id, content=title
            )
        except Exception as e:
            logger.warning("card media send errored; using text fallback: %s", e)
            mid = None
        if mid:
            return mid
        logger.warning("card media send failed; using text fallback")
    fallback = text_fallback if text_fallback is not None else nodes_to_text(nodes)
    try:
        return await sender.send_group_text(group_openid, fallback, msg_id=msg_id)
    except Exception as e:
        logger.error("card text fallback failed: %s", e)
        return None
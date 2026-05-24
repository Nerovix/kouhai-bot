"""/clear — clear the current user's stored context for today's problem."""

from __future__ import annotations

import logging
import re

from .. import registry
from ..registry import CommandDef
from ...eventlog import EVENT_META_KEY
from ...napcat.client import (
    react_emoji,
)
from .submit import enqueue_clear_request

logger = logging.getLogger("kouhai-bot.cmd.clear")


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    stripped = raw_text.strip()
    if not re.fullmatch(r"/clear(?:\s+)?", stripped):
        await react_emoji(message_id, "10060")
        return

    await enqueue_clear_request(
        group_id,
        user_id,
        sender,
        message_id,
        event_log=event.get(EVENT_META_KEY),
    )


def register() -> None:
    registry.register(CommandDef(
        name="clear",
        aliases=[],
        description="清空自己在当前题目的提交与问答上下文",
        usage="",
        handler=handle,
        cooldown=3,
    ))

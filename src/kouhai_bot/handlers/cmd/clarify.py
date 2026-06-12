"""/clarify — ask AI to clarify problem details without spoiling the solution."""

from __future__ import annotations

import logging
import re

from .. import registry
from ..registry import CommandDef
from ...context import get_display_name
from ...eventlog import EVENT_META_KEY
from ...napcat.client import (
    build_plain_message,
    send_group_msg,
    send_private_msg,
)
from ...private_judge import GROUP_SCOPE, PRIVATE_SCOPE
from .submit import enqueue_clarify_request

logger = logging.getLogger("kouhai-bot.cmd.clarify")


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    nickname = get_display_name(sender)
    scope = PRIVATE_SCOPE if event.get("message_type") == "private" else GROUP_SCOPE

    stripped = raw_text.lstrip()
    match = re.match(r'/clarify\s+', stripped)
    if not match:
        if scope == PRIVATE_SCOPE:
            await send_private_msg(user_id, build_plain_message("用法：/clarify 你的问题～"))
        else:
            await send_group_msg(group_id, build_plain_message(
                f"@{nickname} 用法：/clarify 你的问题～"
            ))
        return

    question = stripped[match.end():].strip()
    if not question:
        if scope == PRIVATE_SCOPE:
            await send_private_msg(user_id, build_plain_message("/clarify 后面要写你的问题呀～"))
        else:
            await send_group_msg(group_id, build_plain_message(
                f"@{nickname} /clarify 后面要写你的问题呀～"
            ))
        return

    await enqueue_clarify_request(
        group_id,
        user_id,
        sender,
        message_id,
        question,
        event_log=event.get(EVENT_META_KEY),
        scope=scope,
    )


def register() -> None:
    registry.register(CommandDef(
        name="clarify",
        aliases=["clrf"],
        description="向AI澄清题目细节，只回答题目本身不剧透做法",
        usage="你的问题",
        handler=handle,
        cooldown=10,
    ))

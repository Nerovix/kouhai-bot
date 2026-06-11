"""/guess - compare a user's unsolved-problem idea with the likely answer."""

from __future__ import annotations

import re

from .. import registry
from ..registry import CommandDef
from ...context import get_display_name
from ...eventlog import EVENT_META_KEY
from ...napcat.client import build_plain_message, send_group_msg
from .submit import enqueue_guess_request


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    nickname = get_display_name(sender)

    stripped = raw_text.lstrip()
    match = re.match(r'/guess\s+', stripped)
    if not match:
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} 用法：/guess 你的做法猜想～"
        ))
        return

    guess = stripped[match.end():].strip()
    if not guess:
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} /guess 后面要写你的想法呀～"
        ))
        return

    await enqueue_guess_request(
        group_id,
        user_id,
        sender,
        message_id,
        guess,
        event_log=event.get(EVENT_META_KEY),
    )


def register() -> None:
    registry.register(CommandDef(
        name="guess",
        aliases=[],
        description="未解出时分析你的做法猜想和答案方向的契合度",
        usage="你的做法猜想",
        handler=handle,
        cooldown=10,
    ))

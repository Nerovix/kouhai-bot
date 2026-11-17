"""/clear — clear the current user's stored context for the current problem."""

from __future__ import annotations

import logging
import re

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from .submit import enqueue_clear_request

logger = logging.getLogger("kouhai-bot.cmd.clear")


async def handle(ctx: Ctx) -> None:
    stripped = ctx.raw_text.strip()
    if not re.fullmatch(r"/clear(?:\s+)?", stripped):
        await ctx.sender.react(ctx.group_openid, "👌", msg_id=ctx.message_id)
        return
    await enqueue_clear_request(ctx)


def register() -> None:
    registry.register(CommandDef(
        name="clear",
        aliases=[],
        description="清空自己在当前题目的提交与问答上下文",
        usage="",
        handler=handle,
        cooldown=3,
    ))
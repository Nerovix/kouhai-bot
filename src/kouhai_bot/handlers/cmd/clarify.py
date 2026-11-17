"""/clarify — ask AI to clarify problem details without spoiling the solution."""

from __future__ import annotations

import logging
import re

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from .submit import enqueue_clarify_request

logger = logging.getLogger("kouhai-bot.cmd.clarify")


async def handle(ctx: Ctx) -> None:
    stripped = ctx.raw_text.lstrip()
    match = re.match(r'/clarify\s+', stripped)
    if not match:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, "用法：/clarify 你的问题～",
            msg_id=ctx.message_id,
        )
        return
    question = stripped[match.end():].strip()
    if not question:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, "/clarify 后面要写你的问题呀～",
            msg_id=ctx.message_id,
        )
        return
    await enqueue_clarify_request(ctx, question)


def register() -> None:
    registry.register(CommandDef(
        name="clarify",
        aliases=["clrf"],
        description="向AI澄清题目细节，只回答题目本身不剧透做法",
        usage="你的问题",
        handler=handle,
        cooldown=10,
    ))
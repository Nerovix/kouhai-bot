"""/review — discuss the latest solved problem with AI.

Group-only. Defaults to the group's most recently solved problem (snapshotted
at admission). @mentioned group members' same-problem context is folded in.

Quoted-card targeting of older problems depends on the official incoming
reply-reference format, which is not yet verified (see DECISIONS Q10); for now
/review always targets the latest solved problem.
"""

from __future__ import annotations

import logging
import re

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from .. import state
from .submit import enqueue_review_request

logger = logging.getLogger("kouhai-bot.cmd.review")

_MENTION_RE = re.compile(r"<@!([0-9A-Fa-f]+)>")


def _mentioned_user_ids(ctx: Ctx) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for openid in _MENTION_RE.findall(ctx.raw_text):
        openid = openid.upper()
        if openid == ctx.user_openid.upper() or openid in seen:
            continue
        seen.add(openid)
        result.append(openid)
    return result


async def handle(ctx: Ctx) -> None:
    stripped = ctx.raw_text.lstrip()
    match = re.match(r'/review\s+', stripped)
    if not match:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, "用法：/review 你的问题～",
            msg_id=ctx.message_id,
        )
        return
    question = stripped[match.end():].strip()
    if not question:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid,
            "/review 后面要写你的问题呀，想聊什么直接说～",
            msg_id=ctx.message_id,
        )
        return

    review_pid = await state.latest_solved_pid(ctx.db, ctx.group_openid) or ""
    if not review_pid:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid,
            "还没有已通过的题目可以 review 哦～先做出一道再来聊吧！",
            msg_id=ctx.message_id,
        )
        return

    await enqueue_review_request(
        ctx, question, review_pid, mentioned_user_ids=_mentioned_user_ids(ctx),
    )


def register() -> None:
    registry.register(CommandDef(
        name="review",
        aliases=["rv"],
        description="复盘最近一道已通过题；@群友可带入其上下文",
        usage="你的问题",
        handler=handle,
        cooldown=5,
    ))
"""/review — discuss a solved problem with AI.

Group-only. Defaults to the group's most recently solved problem (snapshotted
at admission). Quoting an earlier problem card targets that problem instead
(official ref_idx→pid map, 实测-verified); @mentioned group members' same-problem
context is folded in.
"""

from __future__ import annotations

import logging
import re

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from .. import state
from .submit import enqueue_review_request
from ...db.repos import posts

logger = logging.getLogger("kouhai-bot.cmd.review")


def _mentioned_user_ids(ctx: Ctx) -> list[str]:
    """Structured mentions[] (实测: inbound @ renders as <@openid>, no '!').
    Skip the bot (is_you/bot), the requester, and duplicates."""
    seen: set[str] = set()
    result: list[str] = []
    self_id = ctx.user_openid.upper()
    for mention in ctx.event.get("mentions") or []:
        if not isinstance(mention, dict):
            continue
        if mention.get("is_you") or mention.get("bot"):
            continue
        openid = str(mention.get("member_openid", "") or "").upper()
        if not openid or openid == self_id or openid in seen:
            continue
        seen.add(openid)
        result.append(openid)
    return result


async def _resolve_review_pid(ctx: Ctx) -> str:
    """A quoted problem card targets its pid when that problem is solved here;
    otherwise fall back to the latest solved problem."""
    ref_idx = str(ctx.event.get("ref_msg_idx", "") or "")
    if ref_idx:
        quoted_pid = await posts.pid_for_ref(ctx.db, ctx.group_openid, ref_idx)
        if quoted_pid and await state.is_problem_solved(ctx.db, ctx.group_openid, quoted_pid):
            return quoted_pid
    return await state.latest_solved_pid(ctx.db, ctx.group_openid) or ""


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

    review_pid = await _resolve_review_pid(ctx)
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
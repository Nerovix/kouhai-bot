"""/range — view or set this group's problem difficulty range.

Any group member may set a per-group override of the global default range.
Parsing is deliberately forgiving about the separator (-, ~, ～, 到, space, …):
it extracts the integers and requires exactly two, ordered min < max, within a
reasonable Codeforces rating band. Anything else gets a friendly usage hint.
"""

from __future__ import annotations

import logging
import re

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from ...db.repos import users
from ...problem_preparation import effective_rating_range

logger = logging.getLogger("kouhai-bot.cmd.range")

_RATING_FLOOR = 0
_RATING_CEIL = 4000
_USAGE = "用法：/range 2000-2600（两个数，小到大；分隔符随意，比如 2000~2600、2000 到 2600）"


def _parse_range(args: str) -> tuple[int, int] | None:
    nums = re.findall(r"\d+", args)
    if len(nums) != 2:
        return None
    lo, hi = int(nums[0]), int(nums[1])
    if not (_RATING_FLOOR <= lo < hi <= _RATING_CEIL):
        return None
    return lo, hi


async def handle(ctx: Ctx) -> None:
    args = ctx.raw_text.lstrip()
    args = re.sub(r"^/range\s*", "", args).strip()

    if not args:
        lo, hi = await effective_rating_range(ctx.group_openid)
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid,
            f"当前难度区间：{lo}–{hi}。用 /range 2000-2600 可以修改本群区间～",
            msg_id=ctx.message_id,
        )
        return

    parsed = _parse_range(args)
    if parsed is None:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, _USAGE, msg_id=ctx.message_id,
        )
        return

    lo, hi = parsed
    await users.set_group_rating(ctx.db, ctx.group_openid, lo, hi)
    await ctx.sender.send_group_at(
        ctx.group_openid, ctx.user_openid,
        f"好嘞，本群难度区间已设为 {lo}–{hi}，下一道题就按这个范围选～",
        msg_id=ctx.message_id,
    )


def register() -> None:
    registry.register(CommandDef(
        name="range",
        aliases=[],
        description="查看或设置本群题目难度区间",
        usage="[最小-最大]",
        handler=handle,
        cooldown=10,
    ))
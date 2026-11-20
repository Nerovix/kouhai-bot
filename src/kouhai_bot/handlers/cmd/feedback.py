"""/feedback — mark the AI's latest delivered reply as unsatisfactory.

Group-only, DB-backed. Targeting reads the per-user last-interaction cache
only (written by the coordinator whenever a replied record is persisted): it
crosses problem switches and survives /clear. Reports are append-only.
"""

from __future__ import annotations

import logging
import re

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from ...db.repos import feedback
from .submit import run_group_state_update

logger = logging.getLogger("kouhai-bot.cmd.feedback")

NOTE_MAX_LEN = 500


async def handle(ctx: Ctx) -> None:
    match = re.fullmatch(r"/feedback(?:\s+(.+))?", ctx.raw_text.strip(), re.DOTALL)
    if match is None:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, "用法：/fb [简短备注]～",
            msg_id=ctx.message_id,
        )
        return
    note = (match.group(1) or "").strip()
    if len(note) > NOTE_MAX_LEN:
        note = note[:NOTE_MAX_LEN]

    try:
        record = await feedback.get_last_interaction(ctx.db, ctx.group_openid, ctx.user_openid)
        if not isinstance(record, dict):
            await ctx.sender.send_group_at(
                ctx.group_openid, ctx.user_openid,
                "这边还没有可以标注的 AI 回复哦～先 /sbm、/clrf 或 /rv 一次，"
                "收到回复后再来 /fb 吧。",
                msg_id=ctx.message_id,
            )
            return

        async def _append() -> int:
            return await feedback.append_bad_report(
                ctx.db,
                group_openid=ctx.group_openid,
                user_openid=ctx.user_openid,
                note=note,
                cmd_message_id=str(ctx.message_id or ""),
                target_pid=str(record.get("pid", "") or ""),
                target_record=dict(record),
            )

        await run_group_state_update(ctx.group_openid, _append)
    except Exception:
        logger.error(
            "failed to record /feedback report (group=%s user=%s)",
            ctx.group_openid, ctx.user_openid, exc_info=True,
        )
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, "记录反馈失败了，联系一下管理员帮帮忙吧～",
            msg_id=ctx.message_id,
        )
        return

    await ctx.sender.react(
        ctx.group_openid, "👌", msg_id=ctx.message_id, user_openid=ctx.user_openid
    )


def register() -> None:
    registry.register(CommandDef(
        name="feedback",
        aliases=["fb"],
        description="标记对AI最新回复不满意，记录反馈供维护复盘",
        usage="[简短备注]",
        handler=handle,
        cooldown=3,
    ))
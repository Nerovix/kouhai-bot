"""/problem, /tag, /scoreboard, /status — group-only, DB-backed read commands."""

from __future__ import annotations

import logging

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from ..runtime import get_runtime
from .. import state
from ..cards import send_problem_card
from ..shared import format_points, high_difficulty_notice
from ...db.repos import posts
from ...render.forward_card import send_forward_card
from ...render.html import text_to_html

logger = logging.getLogger("kouhai-bot.cmd.stubs")


async def _notify(group_openid: str, user_openid: str, message_id: str, text: str) -> None:
    sender = get_runtime().sender
    if user_openid:
        await sender.send_group_at(group_openid, user_openid, text, msg_id=message_id or None)
    else:
        await sender.send_group_text(group_openid, text, msg_id=message_id or None)


# ── /problem ────────────────────────────────────────────────────────────

async def handle_problem(ctx: Ctx) -> None:
    if ctx.raw_text.lstrip() != "/problem":
        return
    await resend_current_problem_group(ctx.group_openid, ctx.user_openid, ctx.message_id)


async def resend_current_problem_group(
    group_openid: str, user_openid: str = "", message_id: str = "",
) -> None:
    """Resend the current problem card (shared by /pb and the poke trigger)."""
    rt = get_runtime()
    db = rt.db
    current = await state.current_problem(db, group_openid)
    if not current:
        await _notify(
            group_openid, user_openid, message_id,
            "暂时不能查看当前题目，可能是暂时还不存在当前题目，试试 /newproblem？",
        )
        return

    card = current.get("card") or {}
    post_msg = card.get("post_msg")
    sample_messages = card.get("sample_messages")
    if isinstance(post_msg, str) and isinstance(sample_messages, list):
        mid = await send_problem_card(
            rt.sender, group_openid,
            post_msg=post_msg,
            sample_messages=[str(s) for s in sample_messages],
            notes_message=str(card.get("notes_message", "") or ""),
            snake_enabled=bool(card.get("snake_enabled", True)),
            msg_id=message_id or None,
            browser=rt.browser,
        )
        if mid:
            pid = str(current.get("today", "") or "")
            ref_idx = rt.sender.ref_idx_for(mid)
            if ref_idx and pid:
                await posts.record_message_ref(db, group_openid, ref_idx, pid, mid)
            notice = high_difficulty_notice(current)
            if notice:
                await rt.sender.send_group_text(group_openid, notice)
            if pid and await state.is_problem_solved(db, group_openid, pid):
                await rt.sender.send_group_text(
                    group_openid, "这题已经通过啦，可以发 /newproblem 挑战下一题～",
                )
            return

    await _notify(
        group_openid, user_openid, message_id,
        "暂时无法重新发送题目，试试 /tag 查看标签吧～",
    )


# ── /tag ────────────────────────────────────────────────────────────────

async def handle_tag(ctx: Ctx) -> None:
    current = await state.current_problem(ctx.db, ctx.group_openid)
    if not current:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, "还没有今日题目～", msg_id=ctx.message_id,
        )
        return
    tags = current.get("tags", [])
    if not tags:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, "当前题目没有 tag 信息～", msg_id=ctx.message_id,
        )
        return
    await ctx.sender.send_group_at(
        ctx.group_openid, ctx.user_openid,
        f"当前题目的 tags：{'、'.join(tags)}", msg_id=ctx.message_id,
    )


# ── /scoreboard ─────────────────────────────────────────────────────────

async def handle_scoreboard(ctx: Ctx) -> None:
    ranked = await posts.group_scoreboard(ctx.db, ctx.group_openid)
    if not ranked:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid,
            "还没有人解出过题目呢，来做第一人吧！🚀", msg_id=ctx.message_id,
        )
        return

    lines = [
        f"📊 累计解题排行榜（共 {len(ranked)} 人）",
        "计分公式：2000=1 分，每 +300 分翻倍（2^((rating-2000)/300)）",
        "",
    ]
    for entry in ranked:
        lines.append(
            f"#{entry['rank']} {entry['nickname']} — "
            f"{entry['solved']} 题 / {format_points(entry['score'])} 分"
        )
    text = "\n".join(lines)

    rt = get_runtime()
    bot_name = rt.sender.bot_name or "算法助手"
    nodes = [{"nickname": bot_name, "content_html": text_to_html(text), "content_text": text}]
    await send_forward_card(
        rt.sender, ctx.group_openid, nodes, title="排行榜",
        msg_id=ctx.message_id, text_fallback=text, browser=rt.browser,
    )


# ── /status ──────────────────────────────────────────────────────────────

async def handle_status(ctx: Ctx) -> None:
    from .newproblem import get_newproblem_status
    from .submit import get_group_lock_status

    status = get_newproblem_status(ctx.group_openid) or get_group_lock_status(ctx.group_openid)
    if status is None:
        await ctx.sender.send_group_text(
            ctx.group_openid, "🟢 bot 当前空闲，没有在处理请求～", msg_id=ctx.message_id,
        )
        return
    cmd = status["command"]
    await ctx.sender.send_group_text(
        ctx.group_openid, f"🔴 bot 当前有 {cmd} 请求还在处理中～", msg_id=ctx.message_id,
    )


# ── Registration ────────────────────────────────────────────────────────

def register() -> None:
    registry.register(CommandDef(
        name="problem", aliases=["pb"],
        description="重新查看当前题目", usage="", handler=handle_problem,
    ))
    registry.register(CommandDef(
        name="tag", aliases=[],
        description="查看当前题目的算法标签", usage="", handler=handle_tag,
    ))
    registry.register(CommandDef(
        name="scoreboard", aliases=[],
        description="累计解题排行", usage="", handler=handle_scoreboard,
    ))
    registry.register(CommandDef(
        name="status", aliases=[],
        description="查看bot当前是否空闲", usage="", handler=handle_status,
    ))
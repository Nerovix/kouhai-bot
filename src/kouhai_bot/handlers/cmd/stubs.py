"""/problem, /tag, /scoreboard, /status — group-only, DB-backed read commands."""

from __future__ import annotations

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from ..runtime import get_runtime
from .. import state
from ..cards import load_prerendered_problem_card, send_problem_card
from ..shared import format_points, high_difficulty_notice
from ... import legacy_migration
from ...db.repos import posts, problems
from ...render.forward_card import send_forward_card
from ...render.html import build_scoreboard_html


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


async def _load_problem_card_cache(rt, db, pid: str) -> bytes | None:
    """Reuse the background pre-rendered card PNG for /pb (no re-render).

    Keyed by pid + statement fingerprint + bot identity, exactly as publication
    and prefetch compute it; a miss (legacy/cold problem) returns None so the
    caller renders fresh. The frozen post_msg in the cache matches card_json.
    """
    if not pid:
        return None
    problem = await problems.get_problem(db, pid)
    sha = str(problem.get("statement_sha256", "") or "") if problem else ""
    if not sha:
        return None
    cached = load_prerendered_problem_card(
        pid, sha,
        bot_name=rt.sender.bot_name or "KouhaiBot",
        bot_avatar_url=rt.sender.bot_avatar_url,
    )
    return cached[0] if cached is not None else None


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
            "暂时不能查看当前题目，可能是暂时还不存在当前题目，试试 /np？",
        )
        return

    card = current.get("card") or {}
    post_msg = card.get("post_msg")
    sample_messages = card.get("sample_messages")
    if isinstance(post_msg, str) and isinstance(sample_messages, list):
        pid = str(current.get("today", "") or "")
        prerendered_png = await _load_problem_card_cache(rt, db, pid)
        mid = await send_problem_card(
            rt.sender, group_openid,
            post_msg=post_msg,
            sample_messages=[str(s) for s in sample_messages],
            notes_message=str(card.get("notes_message", "") or ""),
            snake_enabled=bool(card.get("snake_enabled", True)),
            msg_id=message_id or None,
            browser=rt.browser,
            prerendered_png=prerendered_png,
        )
        if mid:
            ref_idx = rt.sender.ref_idx_for(mid)
            if ref_idx and pid:
                await posts.record_message_ref(db, group_openid, ref_idx, pid, mid)
            notice = high_difficulty_notice(current)
            if notice:
                await rt.sender.send_group_text(group_openid, notice)
            if pid and await state.is_problem_solved(db, group_openid, pid):
                await rt.sender.send_group_text(
                    group_openid, "这题已经通过啦，可以发 /np 挑战下一题～",
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
        "计分：2000 分 = 1 分，每 +300 分翻倍",
    ]
    migration_note = (
        "数据正在迁移中，排名可能不完整。"
        if legacy_migration.is_migration_group(ctx.group_openid)
        else ""
    )
    if migration_note:
        lines.append(migration_note)
    lines.append("")
    for entry in ranked:
        lines.append(
            f"#{entry['rank']} {entry['nickname']} — "
            f"{entry['solved']} 题 / {format_points(entry['score'])} 分"
        )
    text = "\n".join(lines)

    rt = get_runtime()
    bot_name = rt.sender.bot_name or "KouhaiBot"
    nodes = [{
        "nickname": bot_name,
        "content_html": build_scoreboard_html(ranked, migration_note=migration_note),
        "content_text": text,
    }]
    await send_forward_card(
        rt.sender, ctx.group_openid, nodes, title="排行榜",
        msg_id=ctx.message_id, text_fallback=text, browser=rt.browser,
    )


# ── /status ──────────────────────────────────────────────────────────────

async def handle_status(ctx: Ctx) -> None:
    from .newproblem import get_newproblem_status
    from .submit import get_group_active_requests
    from ..context import display_name

    entries = get_group_active_requests(ctx.group_openid)
    np_status = get_newproblem_status(ctx.group_openid)
    if np_status:
        entries.append({
            "nickname": display_name(
                str(np_status.get("user_openid", "") or ""),
                str(np_status.get("nickname", "") or ""),
            ),
            "command": str(np_status.get("command", "newproblem") or "newproblem"),
        })
    if not entries:
        await ctx.sender.send_group_text(
            ctx.group_openid, "🟢 bot 当前空闲，没有在处理请求～", msg_id=ctx.message_id,
        )
        return
    listing = "、".join(f"@{e['nickname']} 的 {e['command']}" for e in entries)
    await ctx.sender.send_group_text(
        ctx.group_openid, f"🔴 目前正在处理的请求有：{listing}～", msg_id=ctx.message_id,
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
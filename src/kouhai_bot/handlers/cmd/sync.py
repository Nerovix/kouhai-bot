"""/sync — pull this user's current-problem history from all other groups.

Group = view: a solve/submission is global (user × problem); each group only
sees problems posted in it. /sync in group G for the current problem P pulls
the user's P records from every other group, materializes them into G's view
(tagged ``synced``, source group hidden), and scores a first blood if the pull
brings a correct verdict to an unsolved G.

Merge is pure-string and zero-LLM: sort by global timestamp, exact-dedup on
(content, result) keeping the earliest, and skip anything G already has — so
re-running /sync is idempotent. Empty source aborts without overwriting.
"""

from __future__ import annotations

import logging
import re

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from .. import state
from ..cards import send_history_card
from ..shared import append_model_tag, format_points
from ...db.repos import posts, submissions
from ...editorial_followup import schedule_post_solve_editorial_followup
from .submit import has_active_problem_request, run_group_state_update

logger = logging.getLogger("kouhai-bot.cmd.sync")


def _problem_label(problem: dict, pid: str) -> str:
    parts = [f"CF{pid}"]
    name = str(problem.get("name", "") or "").strip()
    if name:
        parts.append(name)
    rating = str(problem.get("rating", "") or "").strip()
    if rating and rating != "?":
        parts.append(rating)
    return " ".join(parts)


def _dedup_key(record: dict) -> tuple[str, str]:
    return (str(record.get("content", "") or ""), str(record.get("result", "") or ""))


async def handle(ctx: Ctx) -> None:
    if not re.fullmatch(r"/sync(?:\s+)?", ctx.raw_text.strip()):
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, "用法：/sync", msg_id=ctx.message_id,
        )
        return

    db = ctx.db
    problem = await state.current_problem(db, ctx.group_openid)
    pid = str(problem.get("today", "") or "") if problem else ""
    if not pid:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, "当前这边还没有题目，不能 sync。",
            msg_id=ctx.message_id,
        )
        return

    if has_active_problem_request(
        group_openid=ctx.group_openid, user_openid=ctx.user_openid, pid=pid,
    ):
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid,
            "这题还有请求正在处理中，等它结束后再 /sync 吧。", msg_id=ctx.message_id,
        )
        return

    source = await submissions.get_cross_group_submissions(
        db, ctx.user_openid, pid, ctx.group_openid,
    )
    if not source:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid,
            "其他群没有这道题的历史记录，本次 sync 已取消，没有覆盖任何历史。",
            msg_id=ctx.message_id,
        )
        return

    async def _import() -> tuple[list[dict], str, bool]:
        existing = await submissions.get_user_problem_submissions(
            db, ctx.group_openid, ctx.user_openid, pid,
        )
        seen = {_dedup_key(r) for r in existing}
        imported: list[dict] = []
        for rec in sorted(source, key=lambda r: str(r.get("timestamp", ""))):
            key = _dedup_key(rec)
            if key in seen:
                continue
            seen.add(key)
            await submissions.insert_synced_submission(db, ctx.group_openid, rec)
            imported.append(rec)

        cheer = ""
        scored = False
        correct = next((r for r in imported if r.get("result") == "correct"), None)
        if correct is not None:
            if await state.is_problem_solved(db, ctx.group_openid, pid):
                cheer = "群里已经有人先通过这题了，本次只同步记录，不再加分。"
            else:
                solve = await posts.record_solve(
                    db, ctx.group_openid, ctx.user_openid, pid, ctx.name, source="synced",
                )
                if not solve["duplicate"]:
                    scored = True
                    ranked = await posts.group_scoreboard(db, ctx.group_openid)
                    current = next(
                        (e for e in ranked if e["user_openid"] == ctx.user_openid), None,
                    )
                    rating = await state.problem_rating(db, pid)
                    rank = int(current["rank"]) if current else 1
                    total = format_points(float(current["score"])) if current else "0"
                    gain = format_points(posts.rating_to_points(rating)) if rating is not None else "0"
                    solved = solve["solved_count"]
                    head = (
                        f"恭喜拿下本题一血！🎉 本题 +{gain} 分（共 {solved} 题，总分 {total}），当前第 {rank}"
                        if solve["is_first_blood"]
                        else f"做对了！🎉 本题 +{gain} 分（共 {solved} 题，总分 {total}），当前第 {rank}"
                    )
                    reveal = f"本题来自 {_problem_label(problem, pid)}✨"
                    cheer = append_model_tag(
                        f"{head}\n\n{reveal}", str(correct.get("model_tag", "") or ""),
                    )

        return imported, cheer, scored

    imported, cheer, scored = await run_group_state_update(ctx.group_openid, _import)

    if not imported:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid,
            "其他群这道题的记录这边都已经有了，没有新增可同步的内容～",
            msg_id=ctx.message_id,
        )
        return

    await send_history_card(
        ctx.sender, ctx.group_openid, imported,
        user_name=ctx.name,
        user_avatar_url=str(ctx.event.get("avatar", "") or ""),
        msg_id=ctx.message_id, browser=ctx.browser,
    )
    if cheer:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid, cheer, msg_id=ctx.message_id,
        )
    if scored:
        schedule_post_solve_editorial_followup(ctx.group_openid, pid)
    await ctx.sender.react(
        ctx.group_openid, "👌", msg_id=ctx.message_id, user_openid=ctx.user_openid
    )


def register() -> None:
    registry.register(CommandDef(
        name="sync",
        aliases=[],
        description="把你在其他群对当前题的提交记录同步到本群（含通过加分）",
        usage="",
        handler=handle,
        cooldown=3,
    ))
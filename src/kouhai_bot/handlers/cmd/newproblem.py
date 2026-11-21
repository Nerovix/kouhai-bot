"""/newproblem — post a new problem on demand (group-only, multi-group).

Keeps the deliberate "不鼓励换题" friction: a per-group post lock, a shared
cooldown that starts only after successful delivery, and the exact
``/newproblem --force`` requirement to skip an unsolved problem. Publication
claims the group's one READY prefetch slot, delivers the problem as a long
image, and commits the group's current view + verified summary to the DB only
after delivery succeeds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from ..runtime import get_runtime
from .. import state
from ..cards import (
    build_problem_post_msg,
    load_prerendered_problem_card,
    send_problem_card,
)
from ..shared import high_difficulty_notice
from ...config import get_config
from ...db.repos import posts, problems
from ...editorial_followup import (
    deliver_official_tutorial,
    schedule_prefetch_editorial,
)
from ...official.keyboard import jump_original_keyboard, view_problem_keyboard
from ...problem_prefetch import get_next_problem_prefetcher
from ...problem_preparation import ProblemPreparationError
from ...tutorials import get_verified_official_editorial

logger = logging.getLogger("kouhai-bot.cmd.newproblem")
TZ = timezone(timedelta(hours=8))

_cooldowns: dict[str, float] = {}
_newproblem_locks: dict[str, asyncio.Lock] = {}
_newproblem_active: dict[str, dict] = {}


def _newproblem_lock(group_openid: str) -> asyncio.Lock:
    lock = _newproblem_locks.get(group_openid)
    if lock is None:
        lock = asyncio.Lock()
        _newproblem_locks[group_openid] = lock
    return lock


def get_newproblem_status(group_openid: str) -> dict | None:
    return _newproblem_active.get(group_openid)


async def has_unsolved_problem(group_openid: str) -> bool:
    db = get_runtime().db
    problem = await state.current_problem(db, group_openid)
    if not problem:
        return False
    pid = str(problem.get("today", "") or "")
    return bool(pid) and not await state.is_problem_solved(db, group_openid, pid)


async def _notify(
    group_openid: str, user_openid: str, message_id: str, text: str,
    *, keyboard: dict | None = None,
) -> str | None:
    sender = get_runtime().sender
    if user_openid:
        return await sender.send_group_at(
            group_openid, user_openid, text, msg_id=message_id or None, keyboard=keyboard
        )
    if keyboard is not None:
        return await sender.send_group_markdown(
            group_openid, text, msg_id=message_id or None, keyboard=keyboard
        )
    return await sender.send_group_text(group_openid, text, msg_id=message_id or None)


def _format_forced_skip_notice(problem: object) -> str:
    if not isinstance(problem, dict) or not problem:
        return ""
    pid = str(problem.get("today", "") or "").strip()
    if not pid:
        return ""
    parts = [f"CF{pid}"]
    name = str(problem.get("name", "") or "").strip()
    if name:
        parts.append(name)
    rating = str(problem.get("rating", "") or "").strip()
    if rating and rating != "?":
        parts.append(rating)
    return f"当前题目已被跳过，原题为 {' '.join(parts)}。"


async def _send_forced_skip_followup(group_openid: str, problem: object) -> bool:
    notice = _format_forced_skip_notice(problem)
    if not notice:
        return True
    pid = str(problem.get("today", "") or "").strip()
    sender = get_runtime().sender
    sent = await sender.send_group_markdown(
        group_openid, notice, keyboard=jump_original_keyboard(pid)
    )
    if not sent:
        logger.error("[group_%s] Forced-skip notice send failed", group_openid)
        return False
    try:
        editorial = await get_verified_official_editorial(pid)
        if editorial is not None:
            await deliver_official_tutorial(group_openid, pid)
    except Exception as exc:
        logger.warning(
            "[group_%s] Forced-skip editorial delivery failed for %s: %s",
            group_openid, pid, exc, exc_info=True,
        )
    return True


def _format_wait(seconds: int) -> str:
    """Human-friendly Chinese duration for the cooldown reminder.

    ``seconds`` is always >= 1 (the caller rounds up), so this never renders
    "0 秒". Whole minutes drop the seconds part (300 → "5 分钟").
    """
    if seconds >= 60:
        minutes, secs = divmod(seconds, 60)
        if secs == 0:
            return f"{minutes} 分钟"
        return f"{minutes} 分 {secs} 秒"
    return f"{seconds} 秒"


async def enqueue_new_problem(
    group_openid: str,
    *,
    user_openid: str = "",
    nickname: str = "",
    message_id: str = "",
    command: str,
    force: bool = True,
    quiet: bool = False,
) -> bool:
    """Admit, claim, and post a new problem for commands or quiet triggers."""
    cfg = get_config()
    lock = _newproblem_lock(group_openid)
    if lock.locked():
        if not quiet:
            await _notify(group_openid, user_openid, message_id, "新的题目正在准备中，别急～")
        return False

    await lock.acquire()
    try:
        now = time.monotonic()
        last = _cooldowns.get(group_openid)
        if last is not None:
            elapsed = now - last
        else:
            # No in-memory cooldown (e.g. after restart): fall back to the
            # persisted post time so a restart cannot bypass the cooldown.
            posted_at = await state.current_posted_at(get_runtime().db, group_openid)
            elapsed = (time.time() - float(posted_at)) if posted_at else float("inf")
        if elapsed < cfg.newproblem_cooldown:
            if not quiet:
                remaining = math.ceil(cfg.newproblem_cooldown - elapsed)
                await _notify(
                    group_openid, user_openid, message_id,
                    f"刷新太频繁啦，等 {_format_wait(remaining)}再试哦～",
                )
            return False

        if not force and await has_unsolved_problem(group_openid):
            if not quiet:
                await _notify(
                    group_openid, user_openid, message_id,
                    "当前题目还没有人解出来呢～不能直接刷题哦。\n"
                    "可使用 /pb 查看当前题目。\n"
                    "如果确定要换题，请发 /np --force",
                    keyboard=view_problem_keyboard(),
                )
            return False

        logger.info("[group_%s] %s triggered", group_openid, command)
        _newproblem_active[group_openid] = {
            "group_openid": group_openid,
            "user_openid": user_openid,
            "nickname": nickname,
            "message_id": message_id,
            "command": command,
            "admitted_at": now,
        }
        try:
            posted = await _post_new_problem_locked(
                group_openid,
                user_openid=user_openid,
                message_id=message_id,
                notify_group=not quiet,
                announce_skipped=force,
            )
        except Exception:
            if not quiet:
                raise
            logger.exception("[group_%s] %s post failed", group_openid, command)
            posted = False
        finally:
            _newproblem_active.pop(group_openid, None)
        if posted:
            _cooldowns[group_openid] = time.monotonic()
        return posted
    finally:
        lock.release()


async def _post_new_problem_locked(
    group_openid: str,
    *,
    user_openid: str,
    message_id: str,
    notify_group: bool,
    announce_skipped: bool,
) -> bool:
    rt = get_runtime()
    db = rt.db
    prefetcher = get_next_problem_prefetcher(group_openid)
    try:
        slot = await prefetcher.claim()
    except ProblemPreparationError as exc:
        if notify_group:
            await _notify(
                group_openid, user_openid, message_id,
                f"刷题失败了…{exc.user_message}，连试 3 次都没成功，等一会儿再试试吧😢",
            )
        return False

    try:
        previous_problem = await state.current_problem(db, group_openid)
        prepared = slot.problem
        pid = prepared.pid
        desc = prepared.summary
        model_tag = prepared.model_tag
        sample_messages = list(prepared.sample_messages)
        notes_message = prepared.notes_message

        try:
            schedule_prefetch_editorial(pid)
        except Exception as exc:
            logger.warning(
                "[group_%s] Failed to schedule editorial prefetch for %s: %s",
                group_openid, pid, exc,
            )

        bot_name = rt.sender.bot_name or "KouhaiBot"
        cached = load_prerendered_problem_card(
            pid, prepared.statement_sha256,
            bot_name=bot_name, bot_avatar_url=rt.sender.bot_avatar_url,
        )
        if cached is not None:
            prerendered_png, post_msg = cached
        else:
            prerendered_png = None
            post_msg = build_problem_post_msg(desc, model_tag)

        forced_skip_announced = False
        if announce_skipped:
            forced_skip_announced = bool(_format_forced_skip_notice(previous_problem))
            if not await _send_forced_skip_followup(group_openid, previous_problem):
                return False

        mid = await send_problem_card(
            rt.sender, group_openid,
            post_msg=post_msg,
            sample_messages=sample_messages,
            notes_message=notes_message,
            snake_enabled=True,
            msg_id=message_id or None,
            browser=rt.browser,
            prerendered_png=prerendered_png,
        )
        if not mid:
            logger.error("[group_%s] New problem card send failed", group_openid)
            if notify_group:
                await _notify(group_openid, user_openid, message_id, "新题发送失败，请稍后再试试～")
                if forced_skip_announced:
                    await _notify(
                        group_openid, user_openid, message_id,
                        "新题发送失败，目前仍暂使用原题。请联系管理员。",
                    )
            return False

        card_json = json.dumps({
            "post_msg": post_msg,
            "sample_messages": sample_messages,
            "notes_message": notes_message,
            "snake_enabled": True,
        }, ensure_ascii=False)
        await posts.post_problem(
            db, group_openid, pid,
            posted_at=int(time.time()),
            post_date=datetime.now(TZ).strftime("%Y-%m-%d"),
            card_json=card_json,
        )
        ref_idx = rt.sender.ref_idx_for(mid)
        if ref_idx:
            await posts.record_message_ref(db, group_openid, ref_idx, pid, mid)

        if desc and prepared.summary_status.value == "ready":
            await problems.set_problem_summary(
                db, pid, desc, "verified", prepared.statement_sha256,
            )
        else:
            await problems.set_problem_summary(
                db, pid, "", "incomplete", prepared.statement_sha256,
            )

        notice = high_difficulty_notice(prepared.state)
        if notice:
            await rt.sender.send_group_text(group_openid, notice)

        logger.info("[group_%s] New problem posted ✓ pid=%s", group_openid, pid)
        return True
    finally:
        release_task = asyncio.create_task(prefetcher.release(slot.slot_id))
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            with suppress(asyncio.CancelledError):
                await release_task
            raise


_CMD_NEWPROBLEM = "/newproblem"
_CMD_NEWPROBLEM_FORCE = "/newproblem --force"


async def handle(ctx: Ctx) -> None:
    text = ctx.raw_text.lstrip()
    if text == _CMD_NEWPROBLEM_FORCE:
        await enqueue_new_problem(
            ctx.group_openid, user_openid=ctx.user_openid, nickname=ctx.name,
            message_id=ctx.message_id, command="newproblem --force",
        )
        return
    if text == _CMD_NEWPROBLEM:
        await enqueue_new_problem(
            ctx.group_openid, user_openid=ctx.user_openid, nickname=ctx.name,
            message_id=ctx.message_id, command="newproblem", force=False,
        )
        return
    parts = text.split()
    head = parts[0] if parts else ""
    if head == _CMD_NEWPROBLEM and len(parts) > 1:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid,
            "用法：/np 刷题，/np --force 强制换题",
            msg_id=ctx.message_id,
        )
    elif head[: len(_CMD_NEWPROBLEM)] == _CMD_NEWPROBLEM and head != _CMD_NEWPROBLEM:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid,
            "用法：/np 刷题，/np --force 强制换题",
            msg_id=ctx.message_id,
        )


def register() -> None:
    registry.register(CommandDef(
        name="newproblem",
        aliases=["np"],
        description="来一道新题",
        usage="",
        handler=handle,
        cooldown=0,
    ))
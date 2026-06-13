"""/setproblem — choose the current private-judge problem."""

from __future__ import annotations

import logging
import re
import asyncio

from .. import registry
from ..registry import CommandDef
from ...napcat.client import build_plain_message, send_group_msg, send_private_msg
from ...private_judge import (
    NonFormulaImageProblem,
    copy_records,
    current_group_pid,
    group_problem_history,
    is_group_problem_solved,
    load_private_problem_history,
    mark_private_solved,
    parse_problem_ref,
    problem_id_from_ref,
    replace_private_problem_history,
    resolve_problem_by_pid,
    resolve_random_problem,
    send_problem_card_private,
    set_private_current_problem,
)
from ..shared import get_problem_card_ref_pid, get_today_problem

logger = logging.getLogger("kouhai-bot.cmd.setproblem")

_RANDOM_ARGS = {"random", "rand", "r"}
_UNKNOWN_CARD_REPLY = (
    "这条引用我认不出对应哪道题哦，可能不是题目卡片，"
    "也可能卡片太久了～你可以直接发 /sp 当前群题、/sp random，或者发 CF 题号/链接。"
)


def _strip_command(raw_text: str) -> str:
    text = raw_text.lstrip()
    match = re.match(r"/(?:setproblem|sp)(?:\s+|$)", text)
    if not match:
        return ""
    return text[match.end():].strip()


def _reply_to_message_id(segments: list) -> str:
    for seg in segments:
        if seg.get("type") == "reply":
            return str(seg.get("data", {}).get("id", "") or "")
    return ""


def _history_message(*, private_count: int, group_count: int, copied: bool) -> str:
    if copied:
        return "目前你正在使用来自群聊的历史记录。"
    if private_count and group_count:
        return "你在群聊和 private judge 中都有记录，目前正在使用 private judge 中的记录；想改用群聊记录可在私聊发 /sync。"
    if private_count:
        return "目前你正在使用 private judge 中已有的记录。"
    return "你目前此题的记录是干净的。"


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    if event.get("message_type") != "private":
        await send_group_msg(group_id, build_plain_message(
            "private judge 题目请私聊我使用 /setproblem 设置～"
        ))
        return

    arg = _strip_command(raw_text)
    problem: dict | None = None
    prefer_group_card = False
    from_quoted_card = False
    reply_to = _reply_to_message_id(segments)

    if reply_to and not arg:
        pid = get_problem_card_ref_pid(group_id, reply_to)
        if not pid:
            await send_private_msg(user_id, build_plain_message(_UNKNOWN_CARD_REPLY))
            return
        arg = pid
        from_quoted_card = True

    if not arg:
        problem = get_today_problem(group_id)
        if not problem:
            await send_private_msg(user_id, build_plain_message(
                "当前服务群还没有题目可以同步到 private judge～"
            ))
            return
        prefer_group_card = True
    elif arg.lower() in _RANDOM_ARGS:
        await send_private_msg(user_id, build_plain_message("正在随机挑一道题，稍等一下～"))
        try:
            problem = await asyncio.to_thread(resolve_random_problem, group_id)
        except Exception as e:
            logger.warning("private random problem failed: %s", e, exc_info=True)
            await send_private_msg(user_id, build_plain_message(
                "随机题目暂时拉不到，可能是 Codeforces 或题面缓存不太稳定，稍后再试试？"
            ))
            return
    else:
        parsed = parse_problem_ref(arg)
        if not parsed:
            await send_private_msg(user_id, build_plain_message(
                "我没认出这道题～可以发 CF2234B、2234B、Codeforces 题目链接，"
                "或者 /contest/2233/problem/F 这样的路径。"
            ))
            return
        pid = problem_id_from_ref(*parsed)
        if from_quoted_card:
            await send_private_msg(user_id, build_plain_message("正在设置引用的题目，稍等一下～"))
        else:
            await send_private_msg(user_id, build_plain_message(f"正在设置 CF{pid}，稍等一下～"))
        try:
            problem = await asyncio.to_thread(resolve_problem_by_pid, pid)
        except NonFormulaImageProblem:
            if from_quoted_card:
                await send_private_msg(user_id, build_plain_message(
                    "这道题的题面里包含非公式图片，我现在对这类题目的处理能力有限，"
                    "可能看不完整题意。建议先换一道题～"
                ))
            else:
                await send_private_msg(user_id, build_plain_message(
                    f"CF{pid} 的题面里包含非公式图片，我现在对这类题目的处理能力有限，"
                    "可能看不完整题意。建议先换一道题～"
                ))
            return
        except Exception as e:
            logger.warning("private setproblem resolve failed for %s: %s", pid, e, exc_info=True)
            if from_quoted_card:
                await send_private_msg(user_id, build_plain_message(
                    "这道题的题面暂时拉不到，稍后再试试？"
                ))
            else:
                await send_private_msg(user_id, build_plain_message(
                    f"CF{pid} 的题面暂时拉不到，稍后再试试？"
                ))
            return

    if not problem:
        await send_private_msg(user_id, build_plain_message("题目信息为空，稍后再试试？"))
        return

    set_private_current_problem(user_id, problem)
    pid = str(problem.get("today", "") or "")
    group_records = group_problem_history(group_id, user_id, pid)
    private_records = load_private_problem_history(user_id, pid)
    copied = False
    if not private_records and group_records:
        replace_private_problem_history(user_id, pid, copy_records(group_records))
        copied = True
        private_records = load_private_problem_history(user_id, pid)

    if is_group_problem_solved(group_id, pid):
        mark_private_solved(user_id, pid, source="group")

    await send_problem_card_private(
        user_id,
        group_id,
        problem,
        prefer_group_card=prefer_group_card or pid == current_group_pid(group_id),
    )
    await send_private_msg(user_id, build_plain_message(
        _history_message(
            private_count=len(private_records),
            group_count=len(group_records),
            copied=copied,
        )
    ))


def register() -> None:
    registry.register(CommandDef(
        name="setproblem",
        aliases=["sp"],
        description="设置 private judge 当前题（题号/链接/random；空参数或引用题目卡片）",
        usage="[题号|链接|random]",
        handler=handle,
        cooldown=3,
    ))

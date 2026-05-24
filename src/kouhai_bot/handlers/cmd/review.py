"""/review — discuss the latest solved problem with AI."""

from __future__ import annotations

import logging
import re

from .. import registry
from ..registry import CommandDef
from ..shared import (
    get_latest_solved_problem_id,
    get_problem_card_ref_pid,
    get_today_problem,
    is_already_solved,
)
from ...napcat.client import build_plain_message, send_group_msg
from ...context import get_display_name
from ...eventlog import EVENT_META_KEY
from .submit import enqueue_review_request

logger = logging.getLogger("kouhai-bot.cmd.review")
_UNKNOWN_CARD_REPLY = (
    "这条引用我认不出对应哪道题哦，可能不是题目卡片，"
    "也可能卡片太久了～你直接发 /review 的话，我就默认复盘最近一道已通过题目。"
)


def _mentioned_user_ids(segments: list, *, requester_id: int) -> list[int]:
    from ...config import get_config

    cfg = get_config()
    ignored = {str(cfg.bot_qq), str(requester_id), "all"}
    seen: set[str] = set()
    result: list[int] = []
    for seg in segments:
        if seg.get("type") != "at":
            continue
        qq = str(seg.get("data", {}).get("qq", "") or "").strip()
        if not qq or qq in ignored or not qq.isdigit():
            continue
        normalized = str(int(qq))
        if normalized in ignored or normalized in seen:
            continue
        seen.add(normalized)
        result.append(int(normalized))
    return result


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    nickname = get_display_name(sender)

    stripped = raw_text.lstrip()
    match = re.match(r'/review\s+', stripped)
    if not match:
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} 用法：/review 你的问题～"
        ))
        return

    question = stripped[match.end():].strip()
    if not question:
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} /review 后面要写你的问题呀，想聊什么直接说～"
        ))
        return

    reply_to = ""
    for seg in segments:
        if seg.get("type") == "reply":
            reply_to = str(seg.get("data", {}).get("id", "") or "")
            break

    review_pid = ""
    if reply_to:
        review_pid = get_problem_card_ref_pid(group_id, reply_to)
        if not review_pid:
            await send_group_msg(group_id, build_plain_message(
                f"@{nickname} {_UNKNOWN_CARD_REPLY}"
            ))
            return
        current = get_today_problem(group_id)
        if current and review_pid == str(current.get("today", "") or "") and not is_already_solved(group_id):
            await send_group_msg(group_id, build_plain_message(
                f"@{nickname} 这道是当前题，大家还在想呢～先自己多想想，解出来后再 /review 吧！"
            ))
            return
    else:
        review_pid = get_latest_solved_problem_id(group_id) or ""

    await enqueue_review_request(
        group_id,
        user_id,
        sender,
        message_id,
        question,
        review_pid=review_pid,
        mentioned_user_ids=_mentioned_user_ids(segments, requester_id=user_id),
        event_log=event.get(EVENT_META_KEY),
    )


def register() -> None:
    registry.register(CommandDef(
        name="review",
        aliases=["rv"],
        description="默认复盘上一道已通过题；引用题目卡片可复盘旧题；@群友可带入其上下文",
        usage="你的问题",
        handler=handle,
        cooldown=5,
    ))

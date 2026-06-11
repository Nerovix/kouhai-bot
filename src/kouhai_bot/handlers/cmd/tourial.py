"""/tourial - send one feasible solution after the current problem is solved."""

from __future__ import annotations

import asyncio

from .. import registry
from ..registry import CommandDef
from ..shared import get_today_problem, is_already_solved
from ...config import get_config
from ...context import get_display_name
from ...napcat.client import (
    build_at,
    build_plain_message,
    build_text,
    react_emoji,
    send_group_forward_msg,
    send_group_msg,
    send_private_msg,
)
from ...tutorials import get_editorial_zh_for_group, get_official_editorial


_CHUNK_SIZE = 3000


def _chunks(text: str, size: int = _CHUNK_SIZE) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


async def _send_answer_card(group_id: int, text: str) -> bool:
    cfg = get_config()
    node_ids: list[str] = []
    for chunk in _chunks(text):
        msg_id = await send_private_msg(cfg.bot_qq, build_plain_message(chunk))
        if not msg_id:
            return False
        node_ids.append(str(msg_id))
    await asyncio.sleep(0.5)
    return bool(await send_group_forward_msg(
        group_id,
        [{"type": "node", "data": {"id": node_id}} for node_id in node_ids],
    ))


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    stripped = raw_text.lstrip()
    if stripped.split()[0] not in {"/tourial", "/tutorial"}:
        return

    nickname = get_display_name(sender)
    problem = get_today_problem(group_id)
    pid = str(problem.get("today", "") or "") if problem else ""
    if not pid:
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} 还没有今日题目哦～"
        ))
        return
    if not is_already_solved(group_id):
        await send_group_msg(group_id, [
            build_at(user_id),
            build_text(" 当前题还没解出，答案先封印一下～可以先用 /guess 聊聊你的思路。"),
        ])
        return

    await react_emoji(message_id, "128064")
    editorial = get_official_editorial(pid)
    if not editorial:
        await send_group_msg(group_id, [
            build_at(user_id),
            build_text(" 这题已经解出啦，但我本地还没有抓到可用题解缓存，暂时给不了完整答案。"),
        ])
        return

    answer, _model_tag = await get_editorial_zh_for_group(editorial, pid)
    if not answer:
        await send_group_msg(group_id, [
            build_at(user_id),
            build_text(" 题解翻译失败了，稍后再试试～"),
        ])
        return

    header = "一种可行答案如下：\n\n"
    if await _send_answer_card(group_id, header + answer):
        await send_group_msg(group_id, [
            build_at(user_id),
            build_text(" 可行答案已经放进转发卡片里啦。"),
        ])
        return

    await send_group_msg(group_id, build_plain_message((header + answer)[:3500]))


def register() -> None:
    registry.register(CommandDef(
        name="tourial",
        aliases=["tutorial"],
        description="当前题解出后发送一种可行答案",
        usage="",
        handler=handle,
        cooldown=10,
    ))

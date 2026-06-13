"""/testcd — check private judge submit cooldown for the current group problem."""

from __future__ import annotations

from .. import registry
from ..registry import CommandDef
from ...napcat.client import build_plain_message, send_group_msg, send_private_msg
from ...user_groups import submit_remaining_sec


def _format_friendly_duration(seconds: int) -> str:
    remaining = max(0, int(seconds))
    parts: list[str] = []
    units = [
        (86400, "天"),
        (3600, "小时"),
        (60, "分钟"),
        (1, "秒"),
    ]
    for unit_seconds, label in units:
        value, remaining = divmod(remaining, unit_seconds)
        if value:
            parts.append(f"{value}{label}")
    return "".join(parts) if parts else "0秒"


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    if raw_text.strip() != "/testcd":
        if event.get("message_type") == "private":
            await send_private_msg(user_id, build_plain_message("用法：/testcd"))
        else:
            await send_group_msg(group_id, build_plain_message("/testcd 请在私聊里使用～"))
        return

    if event.get("message_type") != "private":
        await send_group_msg(group_id, build_plain_message("/testcd 请在私聊里使用～"))
        return

    remaining = submit_remaining_sec(user_id, group_id)
    if remaining <= 0:
        text = "你现在可以提交当前群内的题目！"
    else:
        text = f"你在{_format_friendly_duration(remaining)}后才能提交当前群内的题目，先休息一下吧～"
    await send_private_msg(user_id, build_plain_message(text))


def register() -> None:
    registry.register(CommandDef(
        name="testcd",
        aliases=[],
        description="查看当前群题提交 CD",
        usage="",
        handler=handle,
        cooldown=3,
    ))

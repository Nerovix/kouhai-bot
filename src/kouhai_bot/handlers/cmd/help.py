"""/help command — dynamically generated from command registry."""

from .. import registry
from ..registry import CommandDef, all_commands


def _format_command_name(cmd: CommandDef) -> str:
    if not cmd.aliases:
        return f"/{cmd.name}"
    aliases = ", ".join(f"/{alias}" for alias in cmd.aliases)
    return f"/{cmd.name}({aliases})"


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds == 0:
        return "无冷却"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes}分钟"
    return f"{seconds}秒"


def _description_for_help(cmd: CommandDef, newproblem_cooldown: int) -> str:
    if cmd.name == "newproblem":
        cooldown = _format_duration(newproblem_cooldown)
        return f"刷一道新题（未解须 --force；{cooldown}冷却）"
    return cmd.description


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    """Generate help text from registry, deliver as merged-forward card."""
    import asyncio
    from ...config import get_config
    from ...napcat.client import (
        build_plain_message,
        send_group_msg,
        send_private_msg,
        send_group_forward_msg,
    )
    cfg = get_config()
    cmds = all_commands()
    lines = ["可用指令："]
    for cmd in cmds:
        args = f" {cmd.usage}" if cmd.usage else ""
        description = _description_for_help(cmd, cfg.newproblem_cooldown)
        lines.append(f"{_format_command_name(cmd)}{args} — {description}")
    lines.append("")
    lines.append("💡 每天中午12点，如果当前题还没人做出来会提醒大家继续肝；做出来了就自动刷新一道新题～")
    text = "\n".join(lines)
    msg = build_plain_message(text)

    # Send to self → forward to group (merged-forward card)
    self_resp = await send_private_msg(cfg.bot_qq, msg)
    if not self_resp:
        await send_group_msg(group_id, msg)
        return

    await asyncio.sleep(0.5)
    fwd_resp = await send_group_forward_msg(group_id, [
        {"type": "node", "data": {"id": str(self_resp)}},
    ])
    if not fwd_resp:
        await send_group_msg(group_id, msg)


def register() -> None:
    registry.register(CommandDef(
        name="help",
        aliases=[],
        description="显示本帮助",
        usage="",
        handler=handle,
    ))

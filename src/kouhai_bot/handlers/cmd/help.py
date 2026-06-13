"""/help command — dynamically generated from command registry."""

from .. import registry
from ..registry import CommandDef, all_commands

_PRIVATE_HELP_COMMANDS = {
    "setproblem",
    "problem",
    "tag",
    "submit",
    "clarify",
    "review",
    "clear",
    "sync",
    "testcd",
    "status",
    "help",
}
_GROUP_HELP_HIDDEN_COMMANDS = {"setproblem", "sync", "testcd"}


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


def _command_lines(cmds: list[CommandDef], newproblem_cooldown: int) -> list[str]:
    lines: list[str] = []
    for cmd in cmds:
        args = f" {cmd.usage}" if cmd.usage else ""
        description = _description_for_help(cmd, newproblem_cooldown)
        lines.append(f"{_format_command_name(cmd)}{args} — {description}")
    return lines


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
        send_private_forward_msg,
        send_group_forward_msg,
    )
    cfg = get_config()
    cmds = all_commands()
    is_private = event.get("message_type") == "private"
    if is_private:
        visible = [cmd for cmd in cmds if cmd.name in _PRIVATE_HELP_COMMANDS]
        lines = ["private judge 可用指令："]
        lines.extend(_command_lines(visible, cfg.newproblem_cooldown))
        lines.append("")
        lines.append(
            "🔒 private judge：私聊可用 /setproblem(/sp) 选当前群题、CF题号/链接或 random；"
            "private 通过不自动加分，只有当前群题可在群里 /sync 同步。"
            "/sync 会用另一侧记录覆盖当前侧，可能丢掉当前侧这题交流历史；打星用户提交 CD 内只同步 clarify，CD 后可正常同步。"
        )
    else:
        visible = [cmd for cmd in cmds if cmd.name not in _GROUP_HELP_HIDDEN_COMMANDS]
        lines = ["可用指令："]
        lines.extend(_command_lines(visible, cfg.newproblem_cooldown))
        lines.append("")
        lines.append(
            "🔒 private judge：可以私聊我单独选题、提交和复盘；当前群题可同步回来，详细用法请私聊发 /help。"
        )
        lines.append("")
        lines.append("💡 每天中午12点，如果当前题还没人做出来会提醒大家继续肝；做出来了就自动刷新一道新题～")
    text = "\n".join(lines)
    msg = build_plain_message(text)

    if is_private:
        self_resp = await send_private_msg(cfg.bot_qq, msg)
        if self_resp:
            await asyncio.sleep(0.5)
            fwd_resp = await send_private_forward_msg(user_id, [
                {"type": "node", "data": {"id": str(self_resp)}},
            ])
            if fwd_resp:
                return
        await send_private_msg(user_id, msg)
        return

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

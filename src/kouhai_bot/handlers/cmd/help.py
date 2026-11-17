"""/help — dynamically generated from the command registry, delivered as a card."""

from __future__ import annotations

from .. import registry
from ..registry import CommandDef, all_commands
from ..context import Ctx
from ..runtime import get_runtime
from ...config import get_config
from ...render.forward_card import send_forward_card
from ...render.html import text_to_html


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
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def _description_for_help(cmd: CommandDef, newproblem_cooldown: int) -> str:
    if cmd.name == "newproblem":
        return f"刷一道新题（未解须 --force；{_format_duration(newproblem_cooldown)}冷却）"
    return cmd.description


def _help_text() -> str:
    cfg = get_config()
    cooldown = int(getattr(cfg, "newproblem_cooldown", 300))
    lines = ["可用指令："]
    for cmd in all_commands():
        args = f" {cmd.usage}" if cmd.usage else ""
        lines.append(
            f"{_format_command_name(cmd)}{args} — {_description_for_help(cmd, cooldown)}"
        )
    return "\n".join(lines)


async def handle(ctx: Ctx) -> None:
    text = _help_text()
    rt = get_runtime()
    bot_name = rt.sender.bot_name or "算法助手"
    nodes = [{"nickname": bot_name, "content_html": text_to_html(text), "content_text": text}]
    await send_forward_card(
        rt.sender, ctx.group_openid, nodes, title="帮助",
        msg_id=ctx.message_id, text_fallback=text, browser=rt.browser,
    )


def register() -> None:
    registry.register(CommandDef(
        name="help",
        aliases=[],
        description="显示本帮助",
        usage="",
        handler=handle,
    ))
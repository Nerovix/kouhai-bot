"""/help — dynamically generated from the command registry, delivered as a card.

Commands are grouped into themed sections, each rendered as its own bubble in
the long-image chat record. Only the public short trigger is shown; aliased
commands carry a muted "short for <full name>" note. Markdown-style headings,
bullets and bold keep the list readable instead of one cramped text block.
"""

from __future__ import annotations

from .. import registry
from ..registry import CommandDef, all_commands
from ..context import Ctx
from ..runtime import get_runtime
from ...render.forward_card import send_forward_card
from ...render.html import escape

# Themed grouping (canonical name → section). Order here is display order.
_SECTIONS: list[tuple[str, list[str]]] = [
    ("📚 刷题", ["newproblem", "problem", "tag", "range", "scoreboard"]),
    ("✍️ 判题 · 讨论", ["submit", "clarify", "review", "clear"]),
    ("🔧 其他", ["feedback", "sync", "status", "help"]),
]


def _command_parts(cmd: CommandDef) -> tuple[str, str, str]:
    """Return (trigger_with_args, description, short_for_note)."""
    args = f" {cmd.usage}" if cmd.usage else ""
    trigger = f"/{cmd.trigger}{args}"
    short = f"short for {cmd.name}" if cmd.aliases else ""
    return trigger, cmd.description, short


def _section_nodes() -> tuple[list[dict], str]:
    by_name = {cmd.name: cmd for cmd in all_commands()}
    nodes: list[dict] = []
    text_sections: list[str] = []
    bot_name = get_runtime().sender.bot_name or "算法助手"
    for title, names in _SECTIONS:
        cmds = [by_name[n] for n in names if n in by_name]
        if not cmds:
            continue
        li_html: list[str] = []
        li_text: list[str] = []
        for cmd in cmds:
            trigger, desc, short = _command_parts(cmd)
            short_html = f' <span class="short">{escape(short)}</span>' if short else ""
            li_html.append(
                f"<li><strong>{escape(trigger)}</strong> — {escape(desc)}{short_html}</li>"
            )
            short_text = f"（{short}）" if short else ""
            li_text.append(f"• {trigger} — {desc}{short_text}")
        nodes.append({
            "nickname": bot_name,
            "content_html": f"<h3>{escape(title)}</h3><ul>{''.join(li_html)}</ul>",
            "content_text": f"{title}\n" + "\n".join(li_text),
        })
        text_sections.append(f"{title}\n" + "\n".join(li_text))
    return nodes, "\n\n".join(text_sections)


async def handle(ctx: Ctx) -> None:
    nodes, fallback = _section_nodes()
    rt = get_runtime()
    await send_forward_card(
        rt.sender, ctx.group_openid, nodes, title="帮助",
        msg_id=ctx.message_id, text_fallback=fallback, browser=rt.browser,
    )


def register() -> None:
    registry.register(CommandDef(
        name="help",
        aliases=[],
        description="显示本帮助",
        usage="",
        handler=handle,
    ))
"""Official inline-keyboard (message button) builders.

Buttons attach to **markdown** messages only (official API: 仅 markdown 支持
消息按钮); a media/image message cannot carry them, so image cards get a
follow-up button-only markdown message instead.

Action types: 0 = open URL, 1 = callback (clicking fires an INTERACTION_CREATE
event carrying ``data`` back to the bot — no visible chat message), 2 = command
(clicking inserts ``@bot <data>`` into the input box; ``enter=True`` sends it
immediately instead of pre-filling). Command buttons satisfy the dispatcher's
require-@ gate for free; callback buttons are routed through the same dispatch
as an equivalent typed command (see handlers.process_event).

Layout: ``rows`` is a list of rows, each row a list of buttons rendered left to
right sharing the row width — one row with two buttons splits the line, one row
with a single button spans it.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import get_config

_UNSUPPORT_TIPS = "请升级QQ版本"
_PID_RE = re.compile(r"^(\d+)([A-Za-z][A-Za-z0-9]*)$")


def cf_problem_url(pid: str) -> str:
    """Codeforces problemset URL for a pid like ``1856E1`` ("" if unparseable)."""
    m = _PID_RE.match(str(pid or "").strip())
    if not m:
        return ""
    return f"https://codeforces.com/problemset/problem/{m.group(1)}/{m.group(2)}"


def command_button(
    bid: str, label: str, command: str, *, enter: bool = False, style: int = 1
) -> dict[str, Any]:
    return {
        "id": bid,
        "render_data": {"label": label, "style": style},
        "action": {
            "type": 2,
            "permission": {"type": 2},
            "data": command,
            "enter": enter,
            "reply": False,
            "unsupport_tips": _UNSUPPORT_TIPS,
        },
    }


def url_button(bid: str, label: str, url: str, *, style: int = 1) -> dict[str, Any]:
    return {
        "id": bid,
        "render_data": {"label": label, "style": style},
        "action": {
            "type": 0,
            "permission": {"type": 2},
            "data": url,
            "unsupport_tips": _UNSUPPORT_TIPS,
        },
    }


def callback_button(bid: str, label: str, data: str, *, style: int = 1) -> dict[str, Any]:
    """Callback button (type=1): click fires INTERACTION_CREATE with ``data``.

    For no-arg actions (/np, /clear) — no visible ``@bot …`` message is sent;
    the bot acks the interaction and runs the command as if typed.
    """
    return {
        "id": bid,
        "render_data": {"label": label, "style": style},
        "action": {
            "type": 1,
            "permission": {"type": 2},
            "data": data,
            "unsupport_tips": _UNSUPPORT_TIPS,
        },
    }


def keyboard(rows: list[list[dict]], *, bot_appid: int | str | None = None) -> dict[str, Any]:
    content: dict[str, Any] = {"rows": [{"buttons": row} for row in rows]}
    if bot_appid is not None:
        content["bot_appid"] = int(bot_appid)
    return {"content": content}


def _appid() -> int | None:
    raw = str(get_config().app_id or "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def problem_action_keyboard() -> dict[str, Any]:
    """One row, two half-width buttons: pre-fill /sbm and /clrf for typing."""
    return keyboard(
        [[
            command_button("submit", "提交", "/sbm"),
            command_button("clarify", "询问题意", "/clrf"),
        ]],
        bot_appid=_appid(),
    )


def clarify_action_keyboard() -> dict[str, Any]:
    """One row, two half-width buttons: pre-fill /sbm and /clrf after a clarify."""
    return keyboard(
        [[
            command_button("submit", "提交", "/sbm"),
            command_button("clarify", "继续询问", "/clrf"),
        ]],
        bot_appid=_appid(),
    )


def reject_action_keyboard() -> dict[str, Any]:
    """One row, two half-width buttons: retry submit, or clear context now.

    继续提交 pre-fills /sbm (needs the solution text); 清空上下文 is a no-arg
    callback that runs /clear directly without a visible command message.
    """
    return keyboard(
        [[
            command_button("submit", "继续提交", "/sbm"),
            callback_button("clear", "清空上下文", "/clear"),
        ]],
        bot_appid=_appid(),
    )


def jump_original_keyboard(pid: str) -> dict[str, Any] | None:
    """One full-width button opening the original CF problem ("" pid → None)."""
    url = cf_problem_url(pid)
    if not url:
        return None
    return keyboard([[url_button("original", "跳转原题", url)]], bot_appid=_appid())


def view_problem_keyboard() -> dict[str, Any]:
    """One full-width [查看当前题目] callback → /pb (no-arg, runs as if typed).

    Attached to the unsolved-/np rejection so the user can jump straight to the
    current problem card without typing /pb.
    """
    return keyboard(
        [[callback_button("problem", "查看当前题目", "/pb")]], bot_appid=_appid()
    )


def congrats_keyboard(pid: str) -> dict[str, Any]:
    """Congrats row: [跳转原题] (if pid parseable) + [再来一题 → /np callback].

    After an AC the problem is solved, so a plain /np posts the next one. The
    再来一题 button is a no-arg callback (runs /np as if typed, cooldown intact).
    With an unparseable pid the jump button is dropped and 再来一题 spans the row.
    """
    url = cf_problem_url(pid)
    next_btn = callback_button("next", "再来一题", "/np")
    row = [url_button("original", "跳转原题", url), next_btn] if url else [next_btn]
    return keyboard([row], bot_appid=_appid())
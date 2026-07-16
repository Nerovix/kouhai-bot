"""Handlers for OneBot notice events."""

from __future__ import annotations

from ..config import get_config
from ..napcat.client import send_group_poke


async def handle_notice_event(event: dict) -> None:
    """Poke back when the bot is nudged and post a new problem when eligible."""
    cfg = get_config()
    if event.get("notice_type") != "notify" or event.get("sub_type") != "poke":
        return

    group_id = event.get("group_id", 0)
    raw_user_id = event.get("user_id")
    target_id = event.get("target_id", 0)
    if group_id != cfg.current_group or str(target_id) != str(cfg.bot_qq):
        return
    if isinstance(raw_user_id, bool) or not isinstance(raw_user_id, (int, str)):
        return
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        return
    if user_id <= 0 or user_id == int(cfg.bot_qq):
        return

    await send_group_poke(group_id, user_id)

    # Keep command discovery/import timing unchanged until a relevant poke arrives.
    from .cmd import newproblem

    await newproblem.enqueue_new_problem(
        group_id,
        user_id,
        None,
        "",
        command="poke",
        force=False,
        quiet=True,
        prefix="戳一戳刷新🌟",
    )

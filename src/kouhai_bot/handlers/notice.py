"""Handlers for OneBot notice events."""

from __future__ import annotations

import logging

from ..config import get_config
from ..napcat.client import send_group_poke

logger = logging.getLogger("kouhai-bot.notice")


async def handle_notice_event(event: dict) -> None:
    """Poke back when nudged; reshow the current problem if unsolved, else post a new one."""
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

    try:
        if newproblem.has_unsolved_problem(group_id):
            # Current problem not solved yet: show it (same effect as /pb).
            from .cmd.stubs import resend_current_problem_group

            await resend_current_problem_group(group_id, {"user_id": user_id})
            return

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
    except Exception:
        # Poke handlers run as bare detached tasks with no outer wrapper
        # (handlers/__init__.py), so swallow and log everything past the poke-back.
        logger.exception("[group_%s] poke handling failed", group_id)

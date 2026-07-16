"""Handlers for OneBot notice events."""

from __future__ import annotations

import logging
import time

from ..config import get_config
from ..napcat.client import send_group_poke

logger = logging.getLogger("kouhai-bot.handlers.notice")


async def handle_notice_event(event: dict) -> None:
    """Poke back when the bot is nudged and post a new problem when eligible."""
    cfg = get_config()
    if event.get("notice_type") != "notify" or event.get("sub_type") != "poke":
        return

    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    target_id = event.get("target_id", 0)
    if group_id != cfg.current_group or str(target_id) != str(cfg.bot_qq):
        return

    await send_group_poke(group_id, user_id)

    # Keep command discovery/import timing unchanged until a relevant poke arrives.
    from .cmd import newproblem

    if newproblem._has_unsolved_problem(group_id):
        return

    lock = newproblem._newproblem_lock(group_id)
    if lock.locked():
        return

    await lock.acquire()
    try:
        now = time.monotonic()
        last = newproblem._cooldowns.get(group_id, 0)
        if newproblem._has_unsolved_problem(group_id) or now - last < cfg.newproblem_cooldown:
            return

        logger.info("[group_%s] poke triggered new problem", group_id)
        newproblem._newproblem_active[group_id] = {
            "group_id": group_id,
            "user_id": user_id,
            "message_id": "",
            "command": "poke",
            "admitted_at": now,
        }
        try:
            posted = await newproblem._post_new_problem_locked(
                group_id,
                prefix="戳一戳刷新🌟",
                notify_group=True,
            )
        finally:
            newproblem._newproblem_active.pop(group_id, None)
        if posted:
            newproblem._cooldowns[group_id] = time.monotonic()
    finally:
        lock.release()

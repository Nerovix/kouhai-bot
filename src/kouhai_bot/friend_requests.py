"""Automatic approval for service-group friend requests."""

from __future__ import annotations

import asyncio
import logging

from .config import get_config
from .napcat.client import (
    get_doubt_friends_add_requests,
    set_doubt_friends_add_request,
    set_friend_add_request,
)

logger = logging.getLogger("kouhai-bot.friend_requests")


def _to_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def is_service_group_member(group_id: int, user_id: int) -> bool:
    from .napcat import client as napcat_client

    try:
        resp = await napcat_client._http_post(
            "get_group_member_info",
            {"group_id": group_id, "user_id": user_id, "no_cache": True},
        )
        if isinstance(resp, dict) and resp.get("status") == "ok" and isinstance(resp.get("data"), dict):
            return bool(resp["data"])
    except Exception:
        pass
    try:
        resp = await napcat_client._http_post("get_group_member_list", {"group_id": group_id})
        members = resp.get("data", []) if isinstance(resp, dict) and resp.get("status") == "ok" else []
        return any(str(item.get("user_id", "")) == str(user_id) for item in members if isinstance(item, dict))
    except Exception:
        return False


async def handle_friend_request_event(event: dict) -> None:
    """Approve a normal OneBot friend request when the requester is in CURRENT_GROUP."""
    if event.get("request_type") != "friend":
        return

    cfg = get_config()
    flag = str(event.get("flag", "") or "")
    user_id = _to_int(event.get("user_id", 0))

    if not flag or user_id <= 0:
        logger.warning("ignoring malformed friend request event: %s", event.get("raw", event))
        return
    if str(user_id) == str(cfg.bot_qq):
        return

    if not await is_service_group_member(cfg.current_group, user_id):
        logger.info(
            "friend request from user_%s ignored: not a confirmed member of group_%s",
            user_id,
            cfg.current_group,
        )
        return

    if await set_friend_add_request(flag, approve=True):
        logger.info("approved friend request from service group member user_%s", user_id)
    else:
        logger.warning("failed to approve friend request from service group member user_%s", user_id)


async def approve_doubt_friend_requests(*, count: int = 50) -> int:
    """Approve pending doubtful friend requests from service-group members."""
    cfg = get_config()
    approved = 0
    for req in await get_doubt_friends_add_requests(count=count):
        flag = str(req.get("flag", "") or "")
        user_id = _to_int(req.get("uin") or req.get("user_id"))
        if not flag or user_id <= 0:
            logger.warning("ignoring malformed doubtful friend request: %s", req)
            continue
        if str(user_id) == str(cfg.bot_qq):
            continue
        if not await is_service_group_member(cfg.current_group, user_id):
            logger.info(
                "doubtful friend request from user_%s ignored: not a confirmed member of group_%s",
                user_id,
                cfg.current_group,
            )
            continue
        if await set_doubt_friends_add_request(flag, approve=True):
            approved += 1
            logger.info("approved doubtful friend request from service group member user_%s", user_id)
        else:
            logger.warning(
                "failed to approve doubtful friend request from service group member user_%s",
                user_id,
            )
    return approved


async def doubt_friend_request_loop(
    *,
    stop_event: asyncio.Event,
    interval_seconds: float = 60.0,
) -> None:
    """Poll NapCat's doubtful friend request list until shutdown."""
    logger.info("Doubtful friend request poller started")
    while True:
        try:
            await approve_doubt_friend_requests()
        except Exception as e:
            logger.error("doubtful friend request poll failed: %s", e, exc_info=True)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            break
        except asyncio.TimeoutError:
            continue
    logger.info("Doubtful friend request poller stopped")

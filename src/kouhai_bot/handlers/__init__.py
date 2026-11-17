"""Command dispatch for official-native group events (multi-group).

The bot serves every group it receives events from (group = view); there is no
single configured group and no private judge. C2C/notice/poke/friend-request
events have no official equivalent and are ignored. Each parsed slash command
is routed to its handler with a ``Ctx``; per-(group, user, command) cooldowns
are enforced here.
"""

from __future__ import annotations

import asyncio
import logging
import time
import unicodedata
from collections import OrderedDict

from . import registry
from .registry import CommandDef
from .context import Ctx
from .. import echo

logger = logging.getLogger("kouhai-bot.handlers")

# (group_openid, user_openid, command_name) → last accepted monotonic time.
_cooldowns: dict[tuple[str, str, str], float] = {}

# The official gateway may push the same event more than once. Remember recently
# seen message ids (globally unique, ``ROBOT1.0_…``) and drop repeats so a duplicate
# delivery cannot trigger a command twice. Bounded LRU; duplicates arrive close
# together so a few thousand entries cover the replay window comfortably.
_seen_message_ids: OrderedDict[str, None] = OrderedDict()
_SEEN_MESSAGE_IDS_MAX = 8192


def _is_duplicate_message(message_id: str) -> bool:
    if not message_id:
        return False
    if message_id in _seen_message_ids:
        return True
    _seen_message_ids[message_id] = None
    while len(_seen_message_ids) > _SEEN_MESSAGE_IDS_MAX:
        _seen_message_ids.popitem(last=False)
    return False


def _normalize_leading_command_junk(text: str) -> str:
    """Strip invisible leading junk only when it prefixes a slash command.

    Some QQ clients occasionally prepend zero-width/variation-selector codepoints
    before pasted commands such as ``/review``. We only normalize when the first
    visible character after the junk is ``/`` so ordinary messages are unchanged.
    """
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if unicodedata.category(ch) in {"Cf", "Mn"}:
            i += 1
            continue
        break
    if i > 0 and i < len(text) and text[i] == "/":
        return text[i:]
    return text


def _cooldown_remaining(group_openid: str, user_openid: str, cmd: CommandDef) -> float:
    if cmd.cooldown <= 0:
        return 0.0
    key = (group_openid, user_openid, cmd.name)
    now = time.monotonic()
    last = _cooldowns.get(key)
    if last is not None:
        remaining = cmd.cooldown - (now - last)
        if remaining > 0:
            return remaining
    _cooldowns[key] = now
    return 0.0


def reset_cooldowns_for_tests() -> None:
    _cooldowns.clear()


def reset_message_dedup_for_tests() -> None:
    _seen_message_ids.clear()


async def process_event(
    event: dict,
    *,
    spawn_handlers: bool = True,
) -> asyncio.Task | None:
    """Route a normalized official group event to its command handler.

    ``spawn_handlers=True`` keeps the gateway receive loop from waiting on
    long-running handlers. Tests pass ``False`` to await a handler directly.
    """
    if event.get("type") != "message" or event.get("message_type") != "group":
        return None

    group_openid = str(event.get("group_openid", "") or "")
    user_openid = str(event.get("user_openid", "") or "")
    message_id = str(event.get("message_id", "") or "")
    if not group_openid or not user_openid:
        return None

    if _is_duplicate_message(message_id):
        logger.debug("[group_%s] duplicate message %s dropped", group_openid, message_id)
        return None

    content = str(event.get("content", "") or "")
    raw_text = _normalize_leading_command_junk(content.strip())
    if not raw_text:
        return None

    await echo.check_and_echo(
        group_openid=group_openid,
        user_openid=user_openid,
        content=content,
        raw_text=raw_text,
        message_id=message_id,
    )

    if not raw_text.startswith("/"):
        return None

    cmd_token = raw_text.split()[0]
    cmd_name = cmd_token.lstrip("/").lower()
    cmd_def = registry.get(cmd_name)
    if cmd_def is None or cmd_def.handler is None:
        return None

    if _cooldown_remaining(group_openid, user_openid, cmd_def) > 0:
        return None

    handler_raw_text = raw_text
    if cmd_token.lstrip("/") != cmd_def.name:
        handler_raw_text = f"/{cmd_def.name}{raw_text[len(cmd_token):]}"

    author = event.get("author") or {}
    nickname = str(
        event.get("nickname")
        or author.get("member_nickname")
        or author.get("username")
        or ""
    )
    ctx = Ctx(
        group_openid=group_openid,
        user_openid=user_openid,
        message_id=message_id,
        raw_text=handler_raw_text,
        args=handler_raw_text[len(cmd_def.name) + 1:].strip(),
        nickname=nickname,
        event=event,
    )

    logger.info("[group_%s] user_%s → /%s", group_openid, user_openid, cmd_def.name)

    async def _run_handler() -> None:
        try:
            await cmd_def.handler(ctx)
        except Exception as exc:
            logger.error("Handler error for /%s: %s", cmd_def.name, exc, exc_info=True)

    if spawn_handlers:
        return asyncio.create_task(
            _run_handler(), name=f"dispatch_{cmd_def.name}_{message_id}",
        )
    await _run_handler()
    return None


async def dispatch(event: dict) -> None:
    """Gateway dispatch path (spawn handlers so the receive loop never blocks)."""
    await process_event(event, spawn_handlers=True)
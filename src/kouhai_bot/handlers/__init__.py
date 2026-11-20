"""Command dispatch for official-native group events (multi-group).

The bot serves every group it receives events from (group = view); there is no
single configured group and no private judge. C2C/notice/poke/friend-request
events have no official equivalent and are ignored. Commands only trigger when
the message @s the bot (``at_bot`` or a ``mentions`` entry with ``is_you``);
every group message still feeds hot-migration identity capture. Each parsed
slash command is routed to its handler with a ``Ctx``; per-(group, user,
command) cooldowns are enforced here.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from typing import Any

from . import registry
from .registry import CommandDef
from .context import Ctx
from .runtime import get_runtime
from .. import legacy_migration

logger = logging.getLogger("kouhai-bot.handlers")

# (group_openid, user_openid, command_name) → last accepted monotonic time.
_cooldowns: dict[tuple[str, str, str], float] = {}

# The official gateway may push the same event more than once. Remember recently
# seen message ids (globally unique, ``ROBOT1.0_…``) and drop repeats so a duplicate
# delivery cannot trigger a command twice. Bounded LRU; duplicates arrive close
# together so a few thousand entries cover the replay window comfortably.
_seen_message_ids: OrderedDict[str, None] = OrderedDict()
_SEEN_MESSAGE_IDS_MAX = 8192

# Inbound @ renders as ``<@openid>`` (实测: no ``!``). Quoting a bot message
# auto-inserts @bot, so a quoted-card command arrives as ``<@bot> /review …``.
# Strip leading mention markup before the slash check so it is not dropped.
_LEADING_MENTION_RE = re.compile(r"^(?:<@!?[0-9A-Fa-f]+>\s*)+")


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


def _addressed_to_other_bot(event: dict[str, Any]) -> bool:
    """True when the message @s another bot but not us — ignore its commands.

    The official gateway delivers a command sent to a *different* bot as
    GROUP_MESSAGE_CREATE (not AT) with that bot in ``mentions``; stripping the
    leading @ alone would make us answer commands addressed to other bots.
    """
    mentions = event.get("mentions") or []
    dict_mentions = [m for m in mentions if isinstance(m, dict)]
    mentioned_us = any(m.get("is_you") for m in dict_mentions)
    mentioned_other_bot = any(m.get("bot") and not m.get("is_you") for m in dict_mentions)
    return mentioned_other_bot and not mentioned_us


def _mentions_us(event: dict[str, Any]) -> bool:
    """True when the message addresses us, so commands require an @bot.

    Two official delivery forms count: an @-message (``GROUP_AT_MESSAGE_CREATE``
    → ``at_bot``) and a full-message delivery (command chip / quoted bot card)
    whose structured ``mentions`` marks us with ``is_you``. Un-addressed group
    chatter and other bots' commands never trigger us.
    """
    if event.get("at_bot"):
        return True
    return any(
        isinstance(m, dict) and m.get("is_you")
        for m in (event.get("mentions") or [])
    )


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


async def _interaction_to_message(event: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a group inline-keyboard callback into an equivalent command message.

    Routing the synthesized message through the normal dispatch keeps cooldowns,
    the /np unsolved-needs-force friction, and post-lock busy reminders identical
    to typing the command. A callback has no incoming msg_id, so its replies ride
    active quota. The interaction is acked (PUT code=0) so the clicker's client
    stops loading; code=0 only means "received" — the command's own replies carry
    success/failure exactly like a typed command.
    """
    if event.get("interaction_type") != 11:
        return None
    if event.get("scene") not in ("", "group"):
        return None
    group_openid = str(event.get("group_openid", "") or "")
    user_openid = str(event.get("user_openid", "") or "")
    command = str(event.get("button_data", "") or "").strip()
    interaction_id = str(event.get("interaction_id", "") or "")
    if not group_openid or not user_openid or not command.startswith("/"):
        return None
    if _is_duplicate_message(interaction_id):
        logger.debug("[group_%s] duplicate interaction %s dropped", group_openid, interaction_id)
        return None
    try:
        await get_runtime().sender.ack_interaction(interaction_id)
    except Exception as exc:
        logger.warning("interaction ack failed for %s: %s", interaction_id, exc)
    return {
        "type": "message",
        "message_type": "group",
        "event_type": "INTERACTION_CREATE",
        "group_openid": group_openid,
        "user_openid": user_openid,
        "message_id": "",
        "content": command,
        "nickname": "",
        "author": {},
        "mentions": [],
        "at_bot": True,
        "raw": event.get("raw"),
    }


async def process_event(
    event: dict,
    *,
    spawn_handlers: bool = True,
) -> asyncio.Task | None:
    """Route a normalized official group event to its command handler.

    ``spawn_handlers=True`` keeps the gateway receive loop from waiting on
    long-running handlers. Tests pass ``False`` to await a handler directly.
    """
    if event.get("type") == "interaction":
        event = await _interaction_to_message(event)
        if event is None:
            return None

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
    raw_text = _normalize_leading_command_junk(
        _LEADING_MENTION_RE.sub("", content.strip())
    )
    if not raw_text:
        return None

    if _addressed_to_other_bot(event):
        logger.debug("[group_%s] message addressed to another bot ignored", group_openid)
        return None

    author = event.get("author") or {}
    nickname = str(
        event.get("nickname")
        or author.get("member_nickname")
        or author.get("username")
        or ""
    )

    # Hot-migration: capture this member's identity on EVERY service-group
    # message (not just commands) and migrate their legacy data once matched.
    # Fire-and-forget so the receive loop never blocks; other groups no-op.
    legacy_migration.schedule_migration(group_openid, user_openid, nickname)

    # Commands require an @bot. Migration above still captures every message;
    # un-addressed chatter (and other bots' commands) stops here.
    if not _mentions_us(event):
        return None

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
"""Command dispatch — routes parsed QQ messages to handler functions."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

from . import registry
from .registry import CommandDef
from ..config import get_config
from ..eventlog import (
    EVENT_META_KEY,
    log_command_finished,
    log_command_received,
)
from ..friend_requests import handle_friend_request_event, is_service_group_member
from ..napcat.client import (
    build_plain_message,
    build_reply,
    send_group_msg,
    send_private_msg,
    react_emoji,
)

logger = logging.getLogger("kouhai-bot.handlers")

_PRIVATE_ALLOWED_COMMANDS = {
    "setproblem",
    "problem",
    "submit",
    "clarify",
    "review",
    "clear",
    "sync",
    "cd",
    "status",
    "help",
    "tag",
}


# ── Message parsing ─────────────────────────────────────────────────────

def extract_text(segments: list[dict]) -> tuple[str, bool]:
    """Extract plain text from OneBot11 message segments.
    Returns (text, was_mentioned).
    """
    parts = []
    mentioned = False
    cfg = get_config()
    for seg in segments:
        t = seg.get("type", "")
        if t == "text":
            parts.append(seg.get("data", {}).get("text", ""))
        elif t == "at":
            qq = str(seg.get("data", {}).get("qq", ""))
            if qq == str(cfg.bot_qq):
                mentioned = True
                parts.append("")
            else:
                parts.append(f"@{qq}")
    text = " ".join(p.strip() for p in parts if p.strip()).strip()
    return text, mentioned


def should_respond(msg_type: str, was_mentioned: bool) -> bool:
    """Determine if bot should respond to a message."""
    if msg_type == "private":
        return True
    # Group: only respond when mentioned
    return was_mentioned


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


# ── Command dispatch ────────────────────────────────────────────────────

async def process_event(
    event: dict,
    *,
    spawn_handlers: bool = True,
) -> asyncio.Task | None:
    """Route a parsed OneBot11 event to the appropriate handler.

    ``spawn_handlers=True`` keeps the NapCat reverse-WS receive loop from waiting
    for long-running command handlers. Tests can pass ``False`` to await a handler
    directly.
    """
    if event["type"] == "request":
        if spawn_handlers:
            return asyncio.create_task(
                handle_friend_request_event(event),
                name=f"request_{event.get('request_type', 'unknown')}_{event.get('user_id', 0)}",
            )
        await handle_friend_request_event(event)
        return None

    if event["type"] != "message":
        return

    msg_type = event["message_type"]
    user_id = event["user_id"]
    group_id = event.get("group_id", 0)
    sender = event["sender"]
    message_id = event["message_id"]
    segments = event.get("message", [])

    # Skip own messages
    cfg = get_config()
    if str(user_id) == str(cfg.bot_qq):
        return

    # The bot serves exactly one configured group.
    if msg_type == "group" and group_id != cfg.current_group:
        return
    if msg_type == "private":
        group_id = cfg.current_group

    raw_text, was_mentioned = extract_text(segments)
    raw_text = _normalize_leading_command_junk(raw_text)
    if not raw_text:
        return

    # Only respond to commands (starting with /) or DMs
    if not raw_text.startswith("/"):
        if msg_type == "private":
            # DM — respond naturally
            pass  # TODO: chat handler
        return

    # Extract command name
    cmd_token = raw_text.split()[0]
    cmd_name = cmd_token.lstrip("/").lower()
    cmd_def: CommandDef | None = registry.get(cmd_name)

    if cmd_def is None:
        if msg_type == "private":
            await send_private_msg(user_id, build_plain_message(
                "这个 private judge 指令我还不会哦～可用 /help 看支持的命令。"
            ))
        return  # Unknown command, silently ignore

    if msg_type == "private":
        if cmd_def.name not in _PRIVATE_ALLOWED_COMMANDS:
            await send_private_msg(user_id, build_plain_message(
                "这个命令暂时只能在服务群里使用～private judge 可用 /help 查看支持的命令。"
            ))
            return
        if not await is_service_group_member(group_id, user_id):
            await send_private_msg(user_id, build_plain_message(
                "private judge 目前只服务当前服务群成员。如果你已经在群里，稍后再试试。"
            ))
            return

    if cmd_def.handler is None:
        logger.warning(f"Command '{cmd_name}' has no handler")
        return

    # Dispatch
    logger.info(
        f"[group_{group_id}] user_{user_id} → /{cmd_name}"
    )

    handler_raw_text = raw_text
    original_cmd_name = cmd_token.lstrip("/")
    if original_cmd_name != cmd_def.name:
        handler_raw_text = f"/{cmd_def.name}{raw_text[len(cmd_token):]}"

    try:
        if msg_type == "group":
            event[EVENT_META_KEY] = log_command_received(
                group_id=group_id,
                user_id=user_id,
                sender=sender,
                command=cmd_def.name,
                message_id=message_id,
                raw_text=raw_text,
            )
    except Exception as e:
        logger.warning(f"Failed to write received event for /{cmd_name}: {e}")

    async def _run_handler() -> None:
        try:
            await cmd_def.handler(
                group_id=group_id,
                user_id=user_id,
                sender=sender,
                message_id=message_id,
                raw_text=handler_raw_text,
                segments=segments,
                event=event,
            )
            log_command_finished(event.get(EVENT_META_KEY), status="ok")
        except Exception as e:
            logger.error(f"Handler error for /{cmd_name}: {e}", exc_info=True)
            log_command_finished(
                event.get(EVENT_META_KEY),
                status="error",
                extra={"error": str(e)[:500]},
            )

    if spawn_handlers:
        return asyncio.create_task(
            _run_handler(),
            name=f"dispatch_{cmd_name}_{message_id}",
        )
    await _run_handler()
    return None


async def dispatch(event: dict) -> None:
    """Legacy reverse-WS dispatch path."""
    await process_event(event, spawn_handlers=True)

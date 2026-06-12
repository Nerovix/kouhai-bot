"""NapCat OneBot11 client — WebSocket server + HTTP API.

NapCat connects to us via reverse WebSocket. We receive events
and send messages/actions back via HTTP API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

import aiohttp
import websockets
from websockets.server import ServerConnection

from ..config import get_config

logger = logging.getLogger("kouhai-bot.napcat")

# ── HTTP API ────────────────────────────────────────────────────────────

_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()
    return _session


def _http_url(action: str) -> str:
    cfg = get_config()
    return f"http://{cfg.napcat_http_host}:{cfg.napcat_http_port}/{action}"


async def _http_post(action: str, data: dict) -> dict:
    session = await _get_session()
    async with session.post(_http_url(action), json=data) as resp:
        return await resp.json()


def _extract_message_id(action: str, result: dict) -> int | None:
    if not isinstance(result, dict):
        logger.warning("%s returned non-dict response: %r", action, result)
        return None

    data = result.get("data")
    if isinstance(data, dict):
        message_id = data.get("message_id")
        if message_id is not None:
            return message_id

    logger.warning("%s returned no message_id: %s", action, result)
    return None


# ── Message sending ─────────────────────────────────────────────────────

async def send_group_msg(group_id: int, message: list[dict]) -> int | None:
    """Send a message to a group. Returns message_id or None."""
    try:
        result = await _http_post("send_group_msg", {
            "group_id": group_id,
            "message": message,
        })
        return _extract_message_id("send_group_msg", result)
    except Exception as e:
        logger.error(f"send_group_msg failed: {e}", exc_info=True)
        return None


async def send_private_msg(user_id: int, message: list[dict]) -> int | None:
    """Send a private message. Returns message_id or None."""
    try:
        result = await _http_post("send_private_msg", {
            "user_id": user_id,
            "message": message,
        })
        return _extract_message_id("send_private_msg", result)
    except Exception as e:
        logger.error(f"send_private_msg failed: {e}", exc_info=True)
        return None


async def send_private_forward_msg(user_id: int, messages: list[dict]) -> int | None:
    """Forward messages as a merged card to a private chat."""
    try:
        result = await _http_post("send_private_forward_msg", {
            "user_id": user_id,
            "messages": messages,
        })
        return _extract_message_id("send_private_forward_msg", result)
    except Exception as e:
        logger.error(f"send_private_forward_msg failed: {e}", exc_info=True)
        return None


async def set_friend_add_request(flag: str, *, approve: bool = True, remark: str = "") -> bool:
    """Approve or reject a friend request by OneBot request flag."""
    try:
        result = await _http_post("set_friend_add_request", {
            "flag": str(flag),
            "approve": bool(approve),
            "remark": str(remark or ""),
        })
        if isinstance(result, dict) and str(result.get("status", "") or "").lower() in {"", "ok"}:
            return True
        logger.warning("set_friend_add_request returned unexpected payload: %s", result)
        return False
    except Exception as e:
        logger.error("set_friend_add_request failed: %s", e, exc_info=True)
        return False


async def get_doubt_friends_add_requests(*, count: int = 50) -> list[dict]:
    """Return QQ/NapCat's pending doubtful friend requests."""
    try:
        result = await _http_post("get_doubt_friends_add_request", {
            "count": int(count),
        })
        if isinstance(result, dict) and str(result.get("status", "") or "").lower() in {"", "ok"}:
            data = result.get("data", [])
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        logger.warning("get_doubt_friends_add_request returned unexpected payload: %s", result)
    except Exception as e:
        logger.error("get_doubt_friends_add_request failed: %s", e, exc_info=True)
    return []


async def set_doubt_friends_add_request(flag: str, *, approve: bool = True) -> bool:
    """Approve or reject a doubtful friend request by NapCat doubt-request flag."""
    try:
        result = await _http_post("set_doubt_friends_add_request", {
            "flag": str(flag),
            "approve": bool(approve),
        })
        if isinstance(result, dict) and str(result.get("status", "") or "").lower() in {"", "ok"}:
            return True
        logger.warning("set_doubt_friends_add_request returned unexpected payload: %s", result)
        return False
    except Exception as e:
        logger.error("set_doubt_friends_add_request failed: %s", e, exc_info=True)
        return False


async def react_emoji(message_id: str, emoji_id: str) -> None:
    """React with an emoji to a message."""
    try:
        result = await _http_post("set_msg_emoji_like", {
            "message_id": int(message_id),
            "emoji_id": emoji_id,
        })
        if not isinstance(result, dict) or str(result.get("status", "") or "").lower() not in {"", "ok"}:
            logger.warning("set_msg_emoji_like returned unexpected payload: %s", result)
    except Exception as e:
        logger.error(f"react_emoji failed: {e}", exc_info=True)


async def delete_msg(message_id: str) -> None:
    """Delete a message."""
    try:
        result = await _http_post("delete_msg", {"message_id": message_id})
        if not isinstance(result, dict) or str(result.get("status", "") or "").lower() not in {"", "ok"}:
            logger.warning("delete_msg returned unexpected payload: %s", result)
    except Exception:
        logger.exception("delete_msg failed")


async def send_group_forward_msg(group_id: int, messages: list[dict]) -> int | None:
    """Forward messages as a merged card to a group."""
    try:
        result = await _http_post("send_group_forward_msg", {
            "group_id": group_id,
            "messages": messages,
        })
        return _extract_message_id("send_group_forward_msg", result)
    except Exception as e:
        logger.error(f"send_group_forward_msg failed: {e}", exc_info=True)
        return None


# ── Message builders ────────────────────────────────────────────────────

def build_text(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


def build_at(qq: int) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


def build_face(emoji_id: str | int) -> dict:
    return {"type": "face", "data": {"id": str(emoji_id)}}


def build_private_reaction_message(emoji_id: str | int) -> list[dict]:
    emoji = str(emoji_id)
    if emoji == "123":
        return [build_face("123")]
    text = {
        "128064": "👀",
        "289": "[睁眼]",
        "10060": "👌",
    }.get(emoji, "收到～")
    return build_plain_message(text)


def build_plain_message(text: str) -> list[dict]:
    return [build_text(text)]


def build_reply(text: str, reply_to: str) -> list[dict]:
    return [
        {"type": "reply", "data": {"id": reply_to}},
        build_text(text),
    ]


# ── OneBot11 event parsing ──────────────────────────────────────────────

def parse_event(raw: str) -> dict | None:
    """Parse a raw OneBot11 event into a normalized dict."""
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None

    post_type = event.get("post_type", "")
    if post_type == "meta_event":
        return _parse_meta(event)
    elif post_type == "message":
        return _parse_message(event)
    elif post_type == "notice":
        return _parse_notice(event)
    elif post_type == "request":
        return _parse_request(event)
    return None


def _parse_meta(event: dict) -> dict:
    return {
        "type": "meta",
        "meta_type": event.get("meta_event_type", ""),
        "raw": event,
    }


def _parse_message(event: dict) -> dict:
    msg_type = event.get("message_type", "group")
    sender = event.get("sender", {})
    return {
        "type": "message",
        "message_type": msg_type,
        "group_id": event.get("group_id", 0) if msg_type == "group" else 0,
        "user_id": event.get("user_id", 0),
        "sender": {
            "nickname": sender.get("nickname", ""),
            "card": sender.get("card", ""),
            "user_id": sender.get("user_id", 0),
        },
        "message_id": str(event.get("message_id", "")),
        "raw_message": event.get("raw_message", ""),
        "message": event.get("message", []),
        "raw": event,
    }


def _parse_notice(event: dict) -> dict:
    return {
        "type": "notice",
        "notice_type": event.get("notice_type", ""),
        "raw": event,
    }


def _parse_request(event: dict) -> dict:
    return {
        "type": "request",
        "request_type": event.get("request_type", ""),
        "user_id": event.get("user_id", 0),
        "comment": event.get("comment", ""),
        "flag": event.get("flag", ""),
        "raw": event,
    }


# ── WebSocket server ────────────────────────────────────────────────────

# Callback type: async function(event: dict)
EventHandler = callable


class NapCatServer:
    """WebSocket server that NapCat connects to.

    Usage:
        server = NapCatServer(on_event=my_handler)
        await server.start()
    """

    def __init__(self, on_event: EventHandler | None = None):
        self._on_event = on_event
        self._server = None
        self._active_connections: set[ServerConnection] = set()

    async def start(self) -> None:
        cfg = get_config()
        host = cfg.napcat_ws_host
        port = cfg.napcat_ws_port
        logger.info(f"NapCat WS server starting on {host}:{port}")
        self._server = await websockets.serve(
            self._handle_connection, host, port,
            max_size=2**26,
            ping_interval=30,
            ping_timeout=10,
        )
        logger.info(f"NapCat WS server listening on ws://{host}:{port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(self, ws: ServerConnection) -> None:
        remote = ws.remote_address
        logger.info(f"NapCat connected from {remote}")
        self._active_connections.add(ws)

        try:
            async for raw in ws:
                try:
                    event = parse_event(raw)
                    if event and self._on_event:
                        await self._on_event(event)
                except Exception as e:
                    logger.error(f"Event handler error: {e}")
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"NapCat disconnected from {remote}")
        finally:
            self._active_connections.discard(ws)

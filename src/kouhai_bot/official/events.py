"""Normalize official gateway events into the bot's internal shape.

Identity is openid native: group messages carry ``author.member_openid``
(cross-group stable, == C2C ``user_openid``); the group key is
``group_openid``. Message ids are opaque strings (``ROBOT1.0_…``).
"""

from __future__ import annotations

from typing import Any

GROUP_MESSAGE_EVENTS = frozenset({
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_MESSAGE_CREATE",
})
C2C_MESSAGE_EVENTS = frozenset({"C2C_MESSAGE_CREATE"})
MEMBER_ADD_EVENTS = frozenset({"GROUP_MEMBER_ADD"})
MEMBER_REMOVE_EVENTS = frozenset({"GROUP_MEMBER_REMOVE"})


def _group_message(t: str, d: dict[str, Any]) -> dict[str, Any]:
    author = d.get("author") or {}
    return {
        "type": "message",
        "message_type": "group",
        "event_type": t,
        "group_openid": str(d.get("group_openid", "") or ""),
        "user_openid": str(author.get("member_openid", "") or ""),
        "message_id": str(d.get("id", "") or ""),
        "content": str(d.get("content", "") or ""),
        "timestamp": d.get("timestamp", ""),
        "author": author,
        "attachments": d.get("attachments") or [],
        "at_bot": t == "GROUP_AT_MESSAGE_CREATE",
        "raw": d,
    }


def _c2c_message(t: str, d: dict[str, Any]) -> dict[str, Any]:
    author = d.get("author") or {}
    return {
        "type": "message",
        "message_type": "private",
        "event_type": t,
        "group_openid": "",
        "user_openid": str(author.get("user_openid", "") or ""),
        "message_id": str(d.get("id", "") or ""),
        "content": str(d.get("content", "") or ""),
        "timestamp": d.get("timestamp", ""),
        "author": author,
        "raw": d,
    }


def _member_event(t: str, d: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "member_add" if t in MEMBER_ADD_EVENTS else "member_remove",
        "group_openid": str(d.get("group_openid", "") or ""),
        "user_openid": str(d.get("member_openid", "") or d.get("user_openid", "") or ""),
        "timestamp": d.get("timestamp", ""),
        "raw": d,
    }


def _interaction(t: str, d: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "interaction",
        "group_openid": str(d.get("group_openid", "") or ""),
        "user_openid": str(d.get("group_member_openid", "") or d.get("user_openid", "") or ""),
        "interaction_id": str(d.get("id", "") or ""),
        "data": d.get("data") or {},
        "raw": d,
    }


def normalize_event(t: str, d: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(d, dict):
        return None
    if t in GROUP_MESSAGE_EVENTS:
        return _group_message(t, d)
    if t in C2C_MESSAGE_EVENTS:
        return _c2c_message(t, d)
    if t in MEMBER_ADD_EVENTS or t in MEMBER_REMOVE_EVENTS:
        return _member_event(t, d)
    if t == "INTERACTION_CREATE":
        return _interaction(t, d)
    return None
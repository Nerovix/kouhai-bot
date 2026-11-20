"""Normalize official gateway events into the bot's internal shape.

Identity is openid native: group messages carry ``author.member_openid``
(cross-group stable, == C2C ``user_openid``); the group key is
``group_openid``. Message ids are opaque strings (``ROBOT1.0_…``).

Since the 2026-11-16 official event update, the group ``author`` (GroupUser)
also carries ``username`` (QQ nickname), ``member_nickname`` (in-group card),
and ``avatar`` (avatar URL). We surface a resolved ``nickname`` (group card
preferred, then QQ nickname) and ``avatar``; the structured ``mentions`` list
and ``message_scene``/``msg_elements`` (quote/reference context) are passed
through for @-mention and quoted-reply handling.
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


def parse_scene_ext(ext: Any) -> dict[str, str]:
    """``message_scene.ext`` is a list of ``"key=value"`` strings (实测), e.g.
    ``["ref_msg_idx=REFIDX_…", "msg_idx=REFIDX_…", "auth_token=…"]``. Parse it
    into a dict; tolerate a dict form too. Values may contain ``=`` so split
    on the first only."""
    result: dict[str, str] = {}
    if isinstance(ext, dict):
        return {str(k): str(v) for k, v in ext.items()}
    if isinstance(ext, list):
        for item in ext:
            if isinstance(item, str) and "=" in item:
                key, _, value = item.partition("=")
                result[key.strip()] = value
    return result


def _quoted_element(elements: Any) -> dict[str, Any]:
    """The first type-103 (quote) element carries the quoted message's text and
    its own msg_idx (== scene.ext ref_msg_idx)."""
    if isinstance(elements, list):
        for el in elements:
            if isinstance(el, dict) and el.get("message_type") == 103:
                return el
    return {}


def _group_message(t: str, d: dict[str, Any]) -> dict[str, Any]:
    author = d.get("author") or {}
    scene_ext = parse_scene_ext((d.get("message_scene") or {}).get("ext"))
    quoted = _quoted_element(d.get("msg_elements"))
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
        "nickname": str(author.get("member_nickname") or author.get("username") or ""),
        "avatar": str(author.get("avatar") or ""),
        "mentions": d.get("mentions") or [],
        "message_scene": d.get("message_scene") or {},
        "msg_elements": d.get("msg_elements") or [],
        "msg_idx": scene_ext.get("msg_idx", ""),
        "ref_msg_idx": scene_ext.get("ref_msg_idx", "") or str(quoted.get("msg_idx", "") or ""),
        "quoted_text": str(quoted.get("content", "") or ""),
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
    data = d.get("data") or {}
    resolved = data.get("resolved")
    if not isinstance(resolved, dict):
        resolved = {}
    return {
        "type": "interaction",
        "group_openid": str(d.get("group_openid", "") or ""),
        "user_openid": str(d.get("group_member_openid", "") or d.get("user_openid", "") or ""),
        "interaction_id": str(d.get("id", "") or ""),
        "interaction_type": data.get("type") or d.get("type"),
        "scene": str(d.get("scene", "") or ""),
        "button_data": str(resolved.get("button_data", "") or ""),
        "button_id": str(resolved.get("button_id", "") or ""),
        "data": data,
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
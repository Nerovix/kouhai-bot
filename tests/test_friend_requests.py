"""Tests for automatic friend request approval."""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.handlers import process_event
from kouhai_bot.friend_requests import approve_doubt_friend_requests


def _friend_request(user_id: int = 456, flag: str = "flag-123") -> dict:
    return {
        "type": "request",
        "request_type": "friend",
        "user_id": user_id,
        "comment": "hello",
        "flag": flag,
        "raw": {},
    }


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(bot_qq=1, current_group=999999)


def test_friend_request_from_service_group_member_is_approved(monkeypatch):
    calls = []

    async def fake_http_post(action, data):
        assert action == "get_group_member_info"
        assert data["group_id"] == 999999
        assert data["user_id"] == 456
        return {"status": "ok", "data": {"user_id": 456}}

    async def fake_set_friend_add_request(flag, *, approve=True, remark=""):
        calls.append({"flag": flag, "approve": approve, "remark": remark})
        return True

    monkeypatch.setattr("kouhai_bot.friend_requests.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    monkeypatch.setattr("kouhai_bot.friend_requests.set_friend_add_request", fake_set_friend_add_request)

    asyncio.run(process_event(_friend_request(), spawn_handlers=False))

    assert calls == [{"flag": "flag-123", "approve": True, "remark": ""}]


def test_friend_request_from_non_member_is_ignored(monkeypatch):
    calls = []

    async def fake_http_post(action, data):
        if action == "get_group_member_info":
            return {"status": "failed", "data": None}
        if action == "get_group_member_list":
            return {"status": "ok", "data": [{"user_id": 999}]}
        raise AssertionError(action)

    async def fake_set_friend_add_request(flag, *, approve=True, remark=""):
        calls.append(flag)
        return True

    monkeypatch.setattr("kouhai_bot.friend_requests.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    monkeypatch.setattr("kouhai_bot.friend_requests.set_friend_add_request", fake_set_friend_add_request)

    asyncio.run(process_event(_friend_request(), spawn_handlers=False))

    assert calls == []


def test_friend_request_member_lookup_failure_is_ignored(monkeypatch):
    calls = []

    async def fake_http_post(action, data):
        raise RuntimeError("napcat unavailable")

    async def fake_set_friend_add_request(flag, *, approve=True, remark=""):
        calls.append(flag)
        return True

    monkeypatch.setattr("kouhai_bot.friend_requests.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    monkeypatch.setattr("kouhai_bot.friend_requests.set_friend_add_request", fake_set_friend_add_request)

    asyncio.run(process_event(_friend_request(), spawn_handlers=False))

    assert calls == []


def test_non_friend_request_is_ignored(monkeypatch):
    calls = []

    async def fake_set_friend_add_request(flag, *, approve=True, remark=""):
        calls.append(flag)
        return True

    monkeypatch.setattr("kouhai_bot.friend_requests.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.friend_requests.set_friend_add_request", fake_set_friend_add_request)

    asyncio.run(process_event({"type": "request", "request_type": "group", "user_id": 456, "flag": "f"}, spawn_handlers=False))

    assert calls == []


def test_doubt_friend_request_from_service_group_member_is_approved(monkeypatch):
    calls = []

    async def fake_get_doubt_friends_add_requests(*, count=50):
        return [{"flag": "uid-flag", "uin": "456", "source": "QQ群"}]

    async def fake_http_post(action, data):
        assert action == "get_group_member_info"
        assert data["group_id"] == 999999
        assert data["user_id"] == 456
        return {"status": "ok", "data": {"user_id": 456}}

    async def fake_set_doubt_friends_add_request(flag, *, approve=True):
        calls.append({"flag": flag, "approve": approve})
        return True

    monkeypatch.setattr("kouhai_bot.friend_requests.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.friend_requests.get_doubt_friends_add_requests", fake_get_doubt_friends_add_requests)
    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    monkeypatch.setattr("kouhai_bot.friend_requests.set_doubt_friends_add_request", fake_set_doubt_friends_add_request)

    approved = asyncio.run(approve_doubt_friend_requests())

    assert approved == 1
    assert calls == [{"flag": "uid-flag", "approve": True}]


def test_doubt_friend_request_from_non_member_is_ignored(monkeypatch):
    calls = []

    async def fake_get_doubt_friends_add_requests(*, count=50):
        return [{"flag": "uid-flag", "uin": "456"}]

    async def fake_http_post(action, data):
        if action == "get_group_member_info":
            return {"status": "failed", "data": None}
        if action == "get_group_member_list":
            return {"status": "ok", "data": [{"user_id": 999}]}
        raise AssertionError(action)

    async def fake_set_doubt_friends_add_request(flag, *, approve=True):
        calls.append(flag)
        return True

    monkeypatch.setattr("kouhai_bot.friend_requests.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.friend_requests.get_doubt_friends_add_requests", fake_get_doubt_friends_add_requests)
    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    monkeypatch.setattr("kouhai_bot.friend_requests.set_doubt_friends_add_request", fake_set_doubt_friends_add_request)

    approved = asyncio.run(approve_doubt_friend_requests())

    assert approved == 0
    assert calls == []


def test_private_dispatch_uses_shared_service_group_member_check(monkeypatch):
    calls = []

    async def fake_handler(**kwargs):
        calls.append(kwargs)

    async def fake_is_service_group_member(group_id, user_id):
        assert group_id == 999999
        assert user_id == 456
        return True

    monkeypatch.setattr("kouhai_bot.handlers.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.handlers.is_service_group_member", fake_is_service_group_member)
    monkeypatch.setattr(
        "kouhai_bot.handlers.registry.get",
        lambda name: SimpleNamespace(name="status", handler=fake_handler) if name == "status" else None,
    )

    event = {
        "type": "message",
        "message_type": "private",
        "group_id": 0,
        "user_id": 456,
        "sender": {"nickname": "Tester", "card": "", "user_id": 456},
        "message_id": "priv-1",
        "raw_message": "/status",
        "message": [{"type": "text", "data": {"text": "/status"}}],
        "raw": {},
    }

    asyncio.run(process_event(event, spawn_handlers=False))

    assert len(calls) == 1
    assert calls[0]["group_id"] == 999999
    assert calls[0]["user_id"] == 456

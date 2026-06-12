"""Tests for automatic friend request approval."""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.handlers import process_event


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

    monkeypatch.setattr("kouhai_bot.handlers.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    monkeypatch.setattr("kouhai_bot.handlers.set_friend_add_request", fake_set_friend_add_request)

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

    monkeypatch.setattr("kouhai_bot.handlers.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    monkeypatch.setattr("kouhai_bot.handlers.set_friend_add_request", fake_set_friend_add_request)

    asyncio.run(process_event(_friend_request(), spawn_handlers=False))

    assert calls == []


def test_friend_request_member_lookup_failure_is_ignored(monkeypatch):
    calls = []

    async def fake_http_post(action, data):
        raise RuntimeError("napcat unavailable")

    async def fake_set_friend_add_request(flag, *, approve=True, remark=""):
        calls.append(flag)
        return True

    monkeypatch.setattr("kouhai_bot.handlers.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    monkeypatch.setattr("kouhai_bot.handlers.set_friend_add_request", fake_set_friend_add_request)

    asyncio.run(process_event(_friend_request(), spawn_handlers=False))

    assert calls == []


def test_non_friend_request_is_ignored(monkeypatch):
    calls = []

    async def fake_set_friend_add_request(flag, *, approve=True, remark=""):
        calls.append(flag)
        return True

    monkeypatch.setattr("kouhai_bot.handlers.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.handlers.set_friend_add_request", fake_set_friend_add_request)

    asyncio.run(process_event({"type": "request", "request_type": "group", "user_id": 456, "flag": "f"}, spawn_handlers=False))

    assert calls == []

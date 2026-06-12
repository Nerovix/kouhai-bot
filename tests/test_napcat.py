"""Tests for NapCat client message parsing and building."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.napcat.client import (
    build_plain_message,
    build_private_reaction_message,
    build_text,
    get_doubt_friends_add_requests,
    parse_event,
    set_doubt_friends_add_request,
)


def test_parse_group_message():
    raw = """{"post_type":"message","message_type":"group","group_id":123,
"user_id":456,"sender":{"nickname":"TestUser","card":"Test"},"message_id":"abc",
"raw_message":"/help","message":[{"type":"text","data":{"text":"/help"}}]}"""
    event = parse_event(raw)
    assert event is not None
    assert event["type"] == "message"
    assert event["message_type"] == "group"
    assert event["group_id"] == 123
    assert event["user_id"] == 456
    assert event["sender"]["nickname"] == "TestUser"


def test_parse_meta_event():
    raw = '{"post_type":"meta_event","meta_event_type":"heartbeat"}'
    event = parse_event(raw)
    assert event is not None
    assert event["type"] == "meta"


def test_build_plain_message():
    msg = build_plain_message("hello")
    assert len(msg) == 1
    assert msg[0]["type"] == "text"
    assert msg[0]["data"]["text"] == "hello"


def test_build_text():
    seg = build_text("hi")
    assert seg["type"] == "text"
    assert seg["data"]["text"] == "hi"


def test_build_private_troll_reaction_uses_face_123():
    assert build_private_reaction_message("123") == [{"type": "face", "data": {"id": "123"}}]


def test_parse_friend_request_event():
    raw = '{"post_type":"request","request_type":"friend","user_id":456,"comment":"hi","flag":"flag-123"}'
    event = parse_event(raw)
    assert event is not None
    assert event["type"] == "request"
    assert event["request_type"] == "friend"
    assert event["user_id"] == 456
    assert event["comment"] == "hi"
    assert event["flag"] == "flag-123"


def test_parse_invalid_json():
    assert parse_event("not json") is None


def test_get_doubt_friends_add_requests(monkeypatch):
    calls = []

    async def fake_http_post(action, data):
        calls.append((action, data))
        return {"status": "ok", "data": [{"flag": "uid-flag", "uin": "456"}]}

    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)

    import asyncio

    result = asyncio.run(get_doubt_friends_add_requests(count=3))

    assert calls == [("get_doubt_friends_add_request", {"count": 3})]
    assert result == [{"flag": "uid-flag", "uin": "456"}]


def test_set_doubt_friends_add_request(monkeypatch):
    calls = []

    async def fake_http_post(action, data):
        calls.append((action, data))
        return {"status": "ok", "data": None}

    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)

    import asyncio

    assert asyncio.run(set_doubt_friends_add_request("uid-flag", approve=True)) is True
    assert calls == [("set_doubt_friends_add_request", {"flag": "uid-flag", "approve": True})]

"""Tests for NapCat client message parsing and building."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.napcat.client import parse_event, build_plain_message, build_text


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

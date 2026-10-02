"""Regression tests for verifying merged-forward delivery after a false failure."""

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.napcat.client import (
    _forward_node_snippets,
    send_group_forward_msg,
    send_private_forward_msg,
)


BOT_QQ = "3671103281"
CARD_TEXT = '2542 王唯桢: 我们关心每个时刻机器人路径的"包围盒"。模拟出每个扩大"包围盒"的时刻'


def _nodes(text: str = CARD_TEXT) -> list[dict]:
    return [{
        "type": "node",
        "data": {
            "content": [{"type": "text", "data": {"text": text}}],
        },
    }]


def _card_message(*, message_id: int = 1719889981, sender: str = BOT_QQ, timestamp: int | None = None,
                  text: str = CARD_TEXT) -> dict:
    raw = json.dumps({
        "app": "com.tencent.multimsg",
        "meta": {"detail": {"news": [{"text": text}]}},
    })
    return {
        "time": int(time.time()) if timestamp is None else timestamp,
        "message_id": message_id,
        "sender": {"user_id": sender, "nickname": "KouhaiBot"},
        "message": [{"type": "json", "data": {"data": raw}}],
    }


def _patch_config(monkeypatch):
    monkeypatch.setattr(
        "kouhai_bot.napcat.client.get_config",
        lambda: SimpleNamespace(bot_qq=BOT_QQ),
    )
    monkeypatch.setattr("kouhai_bot.napcat.client.FORWARD_VERIFY_DELAY_SEC", 0)


def test_group_failed_but_delivered_returns_real_message_id(monkeypatch):
    calls = []
    card = _card_message()

    async def fake_http_post(action, data):
        calls.append((action, data))
        if action == "send_group_forward_msg":
            return {
                "status": "failed",
                "retcode": 1200,
                "message": "发送转发消息（res_id：abc123）失败",
            }
        assert action == "get_group_msg_history"
        return {"status": "ok", "data": {"messages": [card]}}

    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    _patch_config(monkeypatch)

    result = asyncio.run(send_group_forward_msg(937115758, _nodes()))

    assert result == 1719889981
    assert [action for action, _ in calls].count("send_group_forward_msg") == 1
    assert ("get_group_msg_history", {
        "group_id": 937115758,
        "count": 30,
    }) in calls


def test_group_failed_and_not_found_returns_none(monkeypatch):
    calls = []

    async def fake_http_post(action, data):
        calls.append((action, data))
        if action == "send_group_forward_msg":
            return {"status": "failed", "retcode": 1200}
        return {"status": "ok", "data": {"messages": [
            _card_message(timestamp=int(time.time()) - 3600),
            _card_message(text="unrelated recent message"),
        ]}}

    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    _patch_config(monkeypatch)

    assert asyncio.run(send_group_forward_msg(123, _nodes())) is None
    assert [action for action, _ in calls] == [
        "send_group_forward_msg", "get_group_msg_history",
    ]


def test_group_failed_other_sender_not_a_match(monkeypatch):
    async def fake_http_post(action, data):
        if action == "send_group_forward_msg":
            return {"status": "failed"}
        return {"status": "ok", "data": {"messages": [
            _card_message(sender="987654321"),
        ]}}

    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    _patch_config(monkeypatch)

    assert asyncio.run(send_group_forward_msg(123, _nodes())) is None


def test_group_failed_history_fetch_error_returns_none(monkeypatch):
    calls = []

    async def fake_http_post(action, data):
        calls.append(action)
        if action == "send_group_forward_msg":
            return {"status": "failed"}
        raise RuntimeError("history unavailable")

    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    _patch_config(monkeypatch)

    assert asyncio.run(send_group_forward_msg(123, _nodes())) is None
    assert calls == ["send_group_forward_msg", "get_group_msg_history"]


def test_group_success_skips_history(monkeypatch):
    calls = []

    async def fake_http_post(action, data):
        calls.append(action)
        return {"status": "ok", "data": {"message_id": 42}}

    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)

    assert asyncio.run(send_group_forward_msg(123, _nodes())) == 42
    assert calls == ["send_group_forward_msg"]


def test_private_failed_but_delivered(monkeypatch):
    calls = []
    card = _card_message(message_id=314159)

    async def fake_http_post(action, data):
        calls.append((action, data))
        if action == "send_private_forward_msg":
            return {"status": "failed", "retcode": 1200}
        assert action == "get_friend_msg_history"
        return {"status": "ok", "data": {"messages": [card]}}

    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    _patch_config(monkeypatch)

    assert asyncio.run(send_private_forward_msg(2468, _nodes())) == 314159
    assert ("get_friend_msg_history", {
        "user_id": 2468,
        "count": 30,
    }) in calls


def test_no_text_nodes_skips_verification(monkeypatch):
    calls = []
    messages = [{
        "type": "node",
        "data": {
            "content": [
                {"type": "text", "data": {"text": "short"}},
                {"type": "image", "data": {"file": "x"}},
            ],
        },
    }]

    async def fake_http_post(action, data):
        calls.append(action)
        return {"status": "failed"}

    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", fake_http_post)
    _patch_config(monkeypatch)

    assert _forward_node_snippets(messages) == []
    assert asyncio.run(send_group_forward_msg(123, messages)) is None
    assert calls == ["send_group_forward_msg"]

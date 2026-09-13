"""Unit tests for the /sync history card: per-sender chat-record forward nodes.

The card renders each visible message as its own QQ merged-forward node so it
reads like a 聊天记录: user bubbles carry the user's QQ id + display name,
bot bubbles the bot's account. `format_history_records` is kept as the
plain-text fallback when the forward call fails.
"""

import sys, os, asyncio
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.private_judge import (
    PRIVATE_FORWARD_THRESHOLD,
    build_history_card_nodes,
    send_history_card,
)

UID = 42
BOT = 1
TAG = "『DSv4pro』"


def _record(content: str = "", reply: str = "", **overrides) -> dict:
    record = {
        "timestamp": "2026-09-13T12:00:00+08:00",
        "type": "submit",
        "problem": "542D",
    }
    if content:
        record["content"] = content
    if reply:
        record["reply"] = reply
    record.update(overrides)
    return record


def _node_text(node: dict) -> str:
    return "".join(
        seg.get("data", {}).get("text", "")
        for seg in node.get("data", {}).get("content") or []
        if isinstance(seg, dict) and seg.get("type") == "text"
    )


def _msg_text(message) -> str:
    return "".join(
        seg.get("data", {}).get("text", "")
        for seg in message
        if isinstance(seg, dict) and seg.get("type") == "text"
    )


def _build(records: list[dict]) -> list[dict]:
    return build_history_card_nodes(
        records=records,
        user_id=UID,
        user_display_name="张三",
        bot_qq=BOT,
        bot_display_name="KouhaiBot",
    )


# ═══════════════════════════════════════════════════════════════════════
# build_history_card_nodes: speaker attribution, tags, chunking
# ═══════════════════════════════════════════════════════════════════════

def test_nodes_split_messages_by_sender_with_real_identities():
    nodes = _build([
        _record(content="一稿思路", reply="还要再看看～"),
        _record(content="再改一版", reply="这版对了🎉"),
    ])
    assert [node["type"] for node in nodes] == ["node", "node", "node", "node"]
    assert [node["data"]["user_id"] for node in nodes] == [UID, BOT, UID, BOT]
    assert [node["data"]["nickname"] for node in nodes] == ["张三", "KouhaiBot", "张三", "KouhaiBot"]
    assert [_node_text(node) for node in nodes] == ["一稿思路", "还要再看看～", "再改一版", "这版对了🎉"]
    assert nodes[0]["data"]["content"] == [{"type": "text", "data": {"text": "一稿思路"}}]


def test_nodes_render_model_tags_like_the_text_card():
    nodes = _build([
        _record(content="做法", reply="不错～", model_tag=TAG),
        _record(reply="旧格式回复" + TAG),  # legacy embedded tag, no field
        _record(reply="带标签的旧回复", model_tag=TAG),
    ])
    assert [_node_text(node) for node in nodes] == [
        "做法", "不错～" + TAG, "旧格式回复" + TAG, "带标签的旧回复" + TAG,
    ]


def test_nodes_empty_reply_incorrect_falls_back_to_reason():
    nodes = _build([
        _record(content="做法", reply="", result="incorrect", reason="会 TLE", model_tag=TAG),
    ])
    assert [_node_text(node) for node in nodes] == ["做法", "会 TLE。再想想？🤔" + TAG]


def test_nodes_correct_empty_reply_renders_no_bot_node():
    nodes = _build([
        _record(content="做法", reply="", result="correct", reason="完全正确", model_tag=TAG),
    ])
    assert [_node_text(node) for node in nodes] == ["做法"]


def test_nodes_split_long_messages_across_same_sender_nodes():
    long_text = "y" * 6500
    nodes = _build([_record(content=long_text, reply="短回复")])
    assert [node["data"]["user_id"] for node in nodes] == [UID, UID, UID, BOT]
    user_chunks = [_node_text(node) for node in nodes[:-1]]
    assert [len(chunk) for chunk in user_chunks] == [PRIVATE_FORWARD_THRESHOLD, PRIVATE_FORWARD_THRESHOLD, 500]
    assert "".join(user_chunks) == long_text


def test_nodes_display_name_fallbacks():
    nodes = build_history_card_nodes(
        records=[_record(content="q", reply="a")],
        user_id=UID,
        user_display_name="  ",
        bot_qq=BOT,
        bot_display_name="",
    )
    assert [node["data"]["nickname"] for node in nodes] == ["这位群友", "AI助手"]


# ═══════════════════════════════════════════════════════════════════════
# send_history_card: single custom-node forward; plain-text fallback
# ═══════════════════════════════════════════════════════════════════════

def _cfg() -> SimpleNamespace:
    return SimpleNamespace(data_dir="/tmp/nonexistent-for-unit-test", bot_qq=BOT)


def test_send_history_card_forwards_custom_nodes_in_one_call():
    forwards, texts = [], []

    async def _forward(group_id, messages):
        forwards.append((group_id, messages))
        return 222

    async def _text(group_id, message):
        texts.append((group_id, message))

    with patch("kouhai_bot.private_judge.get_config", return_value=_cfg()), \
         patch("kouhai_bot.private_judge.send_group_forward_msg", _forward), \
         patch("kouhai_bot.private_judge.send_group_msg", _text):
        ok = asyncio.run(send_history_card(
            destination="group",
            user_id=UID,
            group_id=777,
            records=[_record(content="q", reply="a")],
            user_display_name="张三",
            bot_display_name="KouhaiBot",
        ))

    assert ok is True
    assert len(forwards) == 1 and not texts
    group_id, messages = forwards[0]
    assert group_id == 777
    assert [node["data"]["user_id"] for node in messages] == [UID, BOT]
    assert [_node_text(node) for node in messages] == ["q", "a"]


def test_send_history_card_private_destination_uses_private_forward():
    forwards = []

    async def _private_forward(user_id, messages):
        forwards.append((user_id, messages))
        return 333

    async def _noop(*args, **kwargs):
        raise AssertionError("wrong send path used")

    with patch("kouhai_bot.private_judge.get_config", return_value=_cfg()), \
         patch("kouhai_bot.private_judge.send_private_forward_msg", _private_forward), \
         patch("kouhai_bot.private_judge.send_group_forward_msg", _noop), \
         patch("kouhai_bot.private_judge.send_group_msg", _noop), \
         patch("kouhai_bot.private_judge.send_private_msg", _noop):
        ok = asyncio.run(send_history_card(
            destination="private",
            user_id=UID,
            group_id=777,
            records=[_record(content="q", reply="a")],
            user_display_name="张三",
        ))

    assert ok is True
    assert forwards[0][0] == UID


def test_send_history_card_falls_back_to_plain_text_when_forward_fails():
    forward_calls, texts = [], []

    async def _failing_forward(group_id, messages):
        forward_calls.append(messages)
        return None

    async def _text(group_id, message):
        texts.append((group_id, message))

    async def _noop(*args, **kwargs):
        raise AssertionError("wrong send path used")

    with patch("kouhai_bot.private_judge.get_config", return_value=_cfg()), \
         patch("kouhai_bot.private_judge.send_group_forward_msg", _failing_forward), \
         patch("kouhai_bot.private_judge.send_group_msg", _text), \
         patch("kouhai_bot.private_judge.send_private_msg", _noop):
        ok = asyncio.run(send_history_card(
            destination="group",
            user_id=UID,
            group_id=777,
            records=[_record(content="q", reply="a")],
            user_display_name="张三",
        ))

    assert ok is True
    assert len(forward_calls) == 1
    joined = "\n".join(_msg_text(message) for _, message in texts)
    assert "张三在当前的历史记录如下：" in joined
    assert "👤：q" in joined and "🤖：a" in joined

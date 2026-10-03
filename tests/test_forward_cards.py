"""Unit tests for custom forward-node assembly (no self-send dance)."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import kouhai_bot.napcat.client as client
import kouhai_bot.handlers.shared as shared


def test_build_id_node_shape():
    assert client.build_id_node(123) == {
        "type": "node",
        "data": {"id": "123"},
    }


def test_forward_helper_custom_nodes_success_skips_legacy(monkeypatch):
    calls = []

    async def custom(group_id, nodes):
        calls.append(("custom", group_id, nodes))
        return 10

    async def self_send(*args):
        raise AssertionError("legacy self-send must not run")

    monkeypatch.setattr(client, "send_group_forward_msg", custom)
    monkeypatch.setattr(client, "send_private_msg", self_send)
    result = asyncio.run(client.send_forward_card_with_legacy_fallback(
        destination="group", group_id=7, nodes=[{"type": "node"}],
        self_send_messages=[[client.build_text("x")]],
    ))
    assert result == 10
    assert calls == [("custom", 7, [{"type": "node"}])]


def test_forward_helper_custom_failure_uses_legacy_ids(monkeypatch):
    calls = []

    forward_calls = 0

    async def custom(user_id, nodes):
        nonlocal forward_calls
        forward_calls += 1
        calls.append(("custom" if forward_calls == 1 else "private", user_id, nodes))
        return None if forward_calls == 1 else 99

    async def self_send(user_id, message):
        calls.append(("self", user_id, message))
        return len(calls) + 100

    monkeypatch.setattr(client, "send_private_forward_msg", custom)
    monkeypatch.setattr(client, "send_private_msg", self_send)
    monkeypatch.setattr(client, "get_config", lambda: type("C", (), {"bot_qq": 5})())
    monkeypatch.setattr(client, "LEGACY_FORWARD_SELF_SEND_DELAY_SEC", 0)
    result = asyncio.run(client.send_forward_card_with_legacy_fallback(
        destination="private", user_id=8, nodes=[{"type": "node"}],
        self_send_messages=[[client.build_text("a")], [client.build_text("b")]],
    ))
    assert result == 99
    assert [item[0] for item in calls] == ["custom", "self", "self", "private"]
    assert calls[-1][2] == [client.build_id_node(102), client.build_id_node(103)]


def test_forward_helper_legacy_failure_returns_none(monkeypatch):
    async def custom(*args):
        return None

    async def self_send(*args):
        return None

    monkeypatch.setattr(client, "send_group_forward_msg", custom)
    monkeypatch.setattr(client, "send_private_msg", self_send)
    monkeypatch.setattr(client, "get_config", lambda: type("C", (), {"bot_qq": 5})())
    result = asyncio.run(client.send_forward_card_with_legacy_fallback(
        destination="group", group_id=7, nodes=[{"type": "node"}],
        self_send_messages=[[client.build_text("x")]],
    ))
    assert result is None


def test_forward_helper_legacy_forward_failure_returns_none(monkeypatch):
    async def custom(*args):
        return None

    async def self_send(*args):
        return 42

    async def legacy_forward(*args):
        return None

    monkeypatch.setattr(client, "send_group_forward_msg", custom)
    monkeypatch.setattr(client, "send_private_msg", self_send)
    monkeypatch.setattr(client, "send_private_forward_msg", legacy_forward)
    monkeypatch.setattr(client, "get_config", lambda: type("C", (), {"bot_qq": 5})())
    monkeypatch.setattr(client, "LEGACY_FORWARD_SELF_SEND_DELAY_SEC", 0)
    result = asyncio.run(client.send_forward_card_with_legacy_fallback(
        destination="private", user_id=8, nodes=[{"type": "node"}],
        self_send_messages=[[client.build_text("x")]],
    ))
    assert result is None


def test_forward_helper_empty_nodes_goes_directly_to_legacy(monkeypatch):
    calls = []

    async def self_send(user_id, message):
        calls.append(("self", user_id, message))
        return 12

    async def group_forward(group_id, nodes):
        calls.append(("group", group_id, nodes))
        return 13

    monkeypatch.setattr(client, "send_private_msg", self_send)
    monkeypatch.setattr(client, "send_group_forward_msg", group_forward)
    monkeypatch.setattr(client, "get_config", lambda: type("C", (), {"bot_qq": 5})())
    monkeypatch.setattr(client, "LEGACY_FORWARD_SELF_SEND_DELAY_SEC", 0)
    result = asyncio.run(client.send_forward_card_with_legacy_fallback(
        destination="group", group_id=7, nodes=[],
        self_send_messages=[[client.build_text("x")]],
    ))
    assert result == 13
    assert calls == [("self", 5, [client.build_text("x")]), ("group", 7, [client.build_id_node(12)])]


def test_build_node_shape():
    node = client.build_node(
        user_id=42, nickname="昵称", content=[{"type": "text", "data": {"text": "hi"}}]
    )
    assert node == {
        "type": "node",
        "data": {
            "user_id": 42,
            "nickname": "昵称",
            "content": [{"type": "text", "data": {"text": "hi"}}],
        },
    }
    bare = client.build_node(user_id="7", content=[])  # type: ignore[arg-type]
    assert bare["data"] == {"user_id": 7, "nickname": "7", "content": []}
    print("✅ build_node: custom sender-attributed node shape")


def test_resolve_bot_display_name_prefers_group_card(monkeypatch):
    calls = []

    async def fake_http(action, data):
        calls.append(action)
        if action == "get_group_member_info":
            return {"status": "ok", "data": {"card": "群名片", "nickname": "nick"}}
        raise AssertionError("login info must not be needed when the card resolves")

    monkeypatch.setattr(client, "_http_post", fake_http)
    monkeypatch.setattr(client, "get_config", lambda: type("C", (), {"bot_qq": 5})())
    assert asyncio.run(client.resolve_bot_display_name(100)) == "群名片"
    assert calls == ["get_group_member_info"]
    print("✅ resolve_bot_display_name: group card wins")


def test_resolve_bot_display_name_falls_back_and_fails_soft(monkeypatch):
    async def fake_http(action, data):
        if action == "get_group_member_info":
            return {"status": "failed", "data": {}}
        if action == "get_login_info":
            return {"status": "ok", "data": {"nickname": "群友"}}
        raise AssertionError(action)

    monkeypatch.setattr(client, "_http_post", fake_http)
    monkeypatch.setattr(client, "get_config", lambda: type("C", (), {"bot_qq": 5})())
    assert asyncio.run(client.resolve_bot_display_name(100)) == "群友"
    assert asyncio.run(client.resolve_bot_display_name(None)) == "群友"

    async def boom(action, data):
        raise RuntimeError("offline")

    monkeypatch.setattr(client, "_http_post", boom)
    assert asyncio.run(client.resolve_bot_display_name(100)) == ""
    print("✅ resolve_bot_display_name: fallback + fail-soft to empty")


def test_build_problem_card_nodes_components(monkeypatch, tmp_path):
    snake = tmp_path / "snake_trio.jpg"
    snake.write_bytes(b"fakesnake")
    monkeypatch.setattr(shared, "snake_image_path", lambda: str(snake))

    nodes = shared.build_problem_card_nodes(
        post_msg="正文",
        sample_messages=["样例1", "样例2"],
        notes_message="解释",
        snake_enabled=True,
        bot_qq=1,
        bot_name="Bot",
    )
    # post + 2 samples + notes + snake image
    assert len(nodes) == 5, nodes
    assert all(
        n["type"] == "node" and n["data"]["user_id"] == 1 and n["data"]["nickname"] == "Bot"
        for n in nodes
    )
    assert nodes[0]["data"]["content"][0]["data"]["text"] == "正文"
    assert nodes[3]["data"]["content"][0]["data"]["text"] == "解释"
    assert nodes[4]["data"]["content"][0]["type"] == "image"
    assert nodes[4]["data"]["content"][0]["data"]["file"].startswith("base64://")
    print("✅ build_problem_card_nodes: one node per component incl. snake")


def test_build_problem_card_nodes_omits_optional_parts(monkeypatch, tmp_path):
    monkeypatch.setattr(shared, "snake_image_path", lambda: str(tmp_path / "missing.jpg"))

    nodes = shared.build_problem_card_nodes(
        post_msg="正文",
        sample_messages=[],
        notes_message="",
        snake_enabled=True,
        bot_qq=1,
        bot_name="",
    )
    assert len(nodes) == 1
    assert nodes[0]["data"]["nickname"] == "1"  # empty lookup falls back to the QQ
    print("✅ build_problem_card_nodes: missing snake/notes degrade cleanly")

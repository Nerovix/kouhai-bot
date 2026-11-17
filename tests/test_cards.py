"""Card assembly + delivery for the official API (handlers/cards.py).

The legacy NapCat merged-forward card is now a rendered long image:
per-sender chat-record bubbles for history, and greeting/samples/notes/snake
nodes for the problem card. Plain text is the fallback on any render or
upload failure. Covers node assembly, the incorrect-verdict reply fallback,
model-tag render-once, and the media/text delivery paths.

NapCat segment / react_emoji coverage is dropped (no official equivalent).
"""

from __future__ import annotations

import base64

import pytest

from kouhai_bot.config import BotConfig
from kouhai_bot.handlers.cards import (
    _bot_reply_text,
    build_history_card_nodes,
    build_problem_card_nodes,
    history_card_text,
    problem_card_text,
    send_history_card,
    send_problem_card,
)
from kouhai_bot.llm_config import LlmProviderConfig
from kouhai_bot.official import OfficialSender, ReplyBudget
from kouhai_bot.render.html import text_to_html

G = "GROUP1"
TAG = "🐳"
USER_NAME = "Alice"
BOT_NAME = "算法助手"


def _record(content: str = "我的做法", reply: str = "", **overrides) -> dict:
    record = {
        "type": "submit",
        "pid": "542D",
        "content": content,
        "result": "incorrect",
        "reason": "",
        "reply": reply,
        "model_tag": "",
    }
    record.update(overrides)
    return record


@pytest.fixture
def tagged_config(tmp_path, monkeypatch):
    cfg = BotConfig(data_dir=str(tmp_path))
    cfg.llm_smart_providers = [
        LlmProviderConfig(
            name="primary", api_key="test-key",
            base_url="https://llm.example.invalid/v1", model="m", model_tag=TAG,
        )
    ]
    monkeypatch.setattr("kouhai_bot.config._config", cfg)
    return cfg


# ── history card nodes ──────────────────────────────────────────────────


def test_history_nodes_per_sender_bubbles():
    records = [
        _record(reply="判对了", result="correct", model_tag=TAG),
        _record(content="题意疑问", reply="解释如下", type="clarify", result="clarify"),
    ]
    nodes = build_history_card_nodes(records, user_name=USER_NAME, bot_name=BOT_NAME)
    assert [(n["nickname"], n["content_text"]) for n in nodes] == [
        (USER_NAME, "我的做法"),
        (BOT_NAME, "判对了" + TAG),
        (USER_NAME, "题意疑问"),
        (BOT_NAME, "解释如下"),
    ]
    for node in nodes:
        assert node["content_html"] == text_to_html(node["content_text"])


def test_history_nodes_pending_and_superseded_render_user_only():
    records = [
        _record(content="在判的提交", result="pending"),
        _record(content="被替换的提交", result="superseded"),
        _record(content="已回复", reply="回复", result="incorrect"),
    ]
    nodes = build_history_card_nodes(records, user_name=USER_NAME, bot_name=BOT_NAME)
    assert [n["nickname"] for n in nodes] == [USER_NAME, USER_NAME, USER_NAME, BOT_NAME]
    assert [n["content_text"] for n in nodes] == [
        "在判的提交", "被替换的提交", "已回复", "回复",
    ]


def test_history_nodes_skip_non_dict_and_empty_content():
    records = [
        "not a dict",
        None,
        _record(content="", reply="只有bot气泡", result="clarify", type="clarify"),
    ]
    nodes = build_history_card_nodes(records, user_name=USER_NAME, bot_name=BOT_NAME)
    assert len(nodes) == 1
    assert nodes[0]["nickname"] == BOT_NAME
    assert nodes[0]["content_text"] == "只有bot气泡"


def test_history_incorrect_empty_reply_uses_reason_fallback():
    nodes = build_history_card_nodes(
        [_record(reply="", reason="复杂度不对", model_tag=TAG)],
        user_name=USER_NAME, bot_name=BOT_NAME,
    )
    assert nodes[1]["content_text"] == "复杂度不对。再想想？🤔" + TAG
    # no reason either → the generic fallback wording
    nodes = build_history_card_nodes(
        [_record(reply="", reason="")], user_name=USER_NAME, bot_name=BOT_NAME,
    )
    assert nodes[1]["content_text"] == "做法不太对呢。再想想？🤔"


def test_history_reason_only_record_renders_reason():
    nodes = build_history_card_nodes(
        [_record(reply="", reason="判定理由", result="correct", model_tag=TAG)],
        user_name=USER_NAME, bot_name=BOT_NAME,
    )
    assert nodes[1]["content_text"] == "判定理由" + TAG


def test_history_legacy_embedded_tag_rendered_once(tagged_config):
    # legacy records embedded the tag in the reply text itself
    record = _record(reply="判对了" + TAG, result="correct", model_tag=TAG)
    assert _bot_reply_text(record) == "判对了" + TAG
    nodes = build_history_card_nodes([record], user_name=USER_NAME, bot_name=BOT_NAME)
    assert nodes[1]["content_text"] == "判对了" + TAG
    assert nodes[1]["content_text"].count(TAG) == 1


def test_history_source_group_never_shown():
    record = _record(
        reply="回复", result="incorrect",
        group_openid="OTHERGROUP", source_group_openid="OTHERGROUP", synced=True,
    )
    nodes = build_history_card_nodes([record], user_name=USER_NAME, bot_name=BOT_NAME)
    assert all("OTHERGROUP" not in n["content_text"] for n in nodes)
    assert all("OTHERGROUP" not in n["content_html"] for n in nodes)
    text = history_card_text([record], user_name=USER_NAME, bot_name=BOT_NAME)
    assert "OTHERGROUP" not in text


def test_history_card_text_fallback_format():
    records = [
        _record(reply="判对了", result="correct", model_tag=TAG),
        _record(content="在判的提交", result="pending"),
    ]
    text = history_card_text(records, user_name=USER_NAME, bot_name=BOT_NAME)
    assert text == (
        f"{USER_NAME}：我的做法\n\n{BOT_NAME}：判对了{TAG}\n\n{USER_NAME}：在判的提交"
    )


# ── problem card nodes ──────────────────────────────────────────────────


def test_problem_card_nodes_order_samples_notes_snake():
    nodes = build_problem_card_nodes(
        post_msg="来看看这道新题吧！",
        sample_messages=["输入\n1\n输出\n2", "输入\n3\n输出\n6"],
        notes_message="样例解释：略",
        snake_enabled=True,
        bot_name=BOT_NAME,
    )
    assert [n["content_text"] for n in nodes] == [
        "来看看这道新题吧！",
        "输入\n1\n输出\n2",
        "输入\n3\n输出\n6",
        "样例解释：略",
        "🐍",
    ]
    assert all(n["nickname"] == BOT_NAME for n in nodes)
    # the snake is embedded as a data URI so rendering needs no network
    assert nodes[-1]["content_html"].startswith('<img src="data:image/jpeg;base64,')


def test_problem_card_nodes_optional_parts():
    nodes = build_problem_card_nodes(
        post_msg="题面", sample_messages=[], notes_message="", snake_enabled=False,
    )
    assert [n["content_text"] for n in nodes] == ["题面"]
    nodes = build_problem_card_nodes(
        post_msg="题面", sample_messages=["样例"], notes_message="", snake_enabled=True,
    )
    assert [n["content_text"] for n in nodes] == ["题面", "样例", "🐍"]


def test_problem_card_text_fallback_joins_with_blank_lines():
    text = problem_card_text(
        post_msg="题面", sample_messages=["样例1", "样例2"], notes_message="解释",
    )
    assert text == "题面\n\n样例1\n\n样例2\n\n解释"


# ── delivery: render → upload → media, text fallback ────────────────────


class FakeRest:
    def __init__(self, *, upload_response=None):
        self.sent: list[tuple[str, dict]] = []
        self.uploads: list[tuple[str, dict]] = []
        self.upload_response = (
            {"file_info": "FI", "file_uuid": "u", "ttl": 300}
            if upload_response is None else upload_response
        )

    async def send_group_message(self, group_openid, payload):
        self.sent.append((group_openid, payload))
        return {"id": f"M{len(self.sent)}"}

    async def upload_group_media(self, group_openid, payload):
        self.uploads.append((group_openid, payload))
        return self.upload_response


@pytest.fixture
def sender(db):
    return OfficialSender(FakeRest(), ReplyBudget(db), bot_name=BOT_NAME)


@pytest.fixture
def no_render(monkeypatch):
    async def _fail(*args, **kwargs):
        raise RuntimeError("render disabled in tests")

    monkeypatch.setattr("kouhai_bot.render.forward_card.render_card_png", _fail)


@pytest.fixture
def fake_render(monkeypatch):
    captured: list[dict] = []

    async def _png(nodes, **kwargs):
        captured.append({"nodes": nodes, **kwargs})
        return b"PNG"

    monkeypatch.setattr("kouhai_bot.render.forward_card.render_card_png", _png)
    return captured


HISTORY_RECORDS = [_record(reply="判对了", result="correct", model_tag=TAG)]


async def test_send_history_card_empty_records_sends_nothing(sender):
    assert await send_history_card(sender, G, [], user_name=USER_NAME) is None
    assert sender._rest.sent == []


async def test_send_history_card_render_failure_falls_back_to_text(sender, no_render):
    mid = await send_history_card(
        sender, G, HISTORY_RECORDS, user_name=USER_NAME, msg_id="IN1",
    )
    assert mid == "M1"
    group, payload = sender._rest.sent[0]
    assert group == G
    assert payload["msg_type"] == 0
    assert payload["content"] == history_card_text(
        HISTORY_RECORDS, user_name=USER_NAME, bot_name=BOT_NAME,
    )
    assert payload["msg_id"] == "IN1"
    assert sender._rest.uploads == []


async def test_send_history_card_media_path(sender, fake_render):
    mid = await send_history_card(sender, G, HISTORY_RECORDS, user_name=USER_NAME)
    assert mid == "M1"
    assert fake_render[0]["title"] == "聊天记录"
    group, up = sender._rest.uploads[0]
    assert group == G
    assert up["file_type"] == 1
    assert up["file_data"] == base64.b64encode(b"PNG").decode("ascii")
    assert up["srv_send_msg"] is False
    _group, payload = sender._rest.sent[0]
    assert payload["msg_type"] == 7
    assert payload["media"] == {"file_info": "FI"}
    assert payload["content"] == "聊天记录"


async def test_send_history_card_upload_without_file_info_falls_back(sender, fake_render):
    sender._rest.upload_response = {}
    mid = await send_history_card(sender, G, HISTORY_RECORDS, user_name=USER_NAME)
    assert mid == "M1"
    payload = sender._rest.sent[0][1]
    assert payload["msg_type"] == 0
    assert payload["content"] == history_card_text(
        HISTORY_RECORDS, user_name=USER_NAME, bot_name=BOT_NAME,
    )


async def test_send_problem_card_media_path_includes_snake_node(sender, fake_render):
    mid = await send_problem_card(
        sender, G,
        post_msg="题面", sample_messages=["样例"], notes_message="解释",
        snake_enabled=True,
    )
    assert mid == "M1"
    assert fake_render[0]["title"] == "题目"
    nodes = fake_render[0]["nodes"]
    assert [n["content_text"] for n in nodes] == ["题面", "样例", "解释", "🐍"]
    payload = sender._rest.sent[0][1]
    assert payload["msg_type"] == 7
    assert payload["content"] == "题目"


async def test_send_problem_card_render_failure_falls_back_to_text(sender, no_render):
    mid = await send_problem_card(
        sender, G,
        post_msg="题面", sample_messages=["样例1", "样例2"], notes_message="解释",
    )
    assert mid == "M1"
    payload = sender._rest.sent[0][1]
    assert payload["msg_type"] == 0
    assert payload["content"] == "题面\n\n样例1\n\n样例2\n\n解释"